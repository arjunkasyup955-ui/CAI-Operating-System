"""Exercises every Phase 0 kernel piece end to end: Event Bus, Agent Registry, Tool
Registry (retry + timeout + cost tracking + schema validation + permission enforcement),
Model Router (incl. live Ollama routing), Memory Gateway (working + vector + knowledge
schema), Permission Layer, Approval Framework (real interrupt/resume + persistence across
a process restart), node-level LangGraph RetryPolicy, and Budget Manager.

Run: python scripts/smoke_test_phase0.py
"""

import hashlib
import logging
import subprocess
import sqlite3
import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from pydantic import BaseModel, ValidationError  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command  # noqa: E402
from langgraph.types import RetryPolicy as NodeRetryPolicy  # noqa: E402

import agents.manager.agent  # noqa: E402  (import registers the manager manifest)
from core.budget_manager import get_budget_manager  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.event_bus import AFOSEvent, get_event_bus  # noqa: E402
from core.memory_gateway import get_memory_gateway  # noqa: E402
from core.memory_gateway.vector_store import EMBEDDING_DIM, VectorStore  # noqa: E402
from core.permissions import Permission, PermissionDeniedError, get_permission_gate  # noqa: E402
from core.registries import ToolSpec, get_agent_registry, get_tool_registry  # noqa: E402
from core.registries.tool_registry import RetryPolicy  # noqa: E402
from workflows.main_graph import compile_main_graph  # noqa: E402

VENTURE_ID = "venture-smoke-test"
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"smoke test failed at: {label}")


def fake_embed(text: str) -> list[float]:
    """Deterministic pseudo-embedding so the vector store's storage/query mechanics can
    be verified without network access or an OPENAI_API_KEY. Production code path
    (VectorStore's default embed_fn) uses real OpenAI embeddings - see providers.py.
    """
    seed = int(hashlib.sha256(text.encode()).hexdigest(), 16) & 0xFFFFFFFF
    vec = []
    for _ in range(EMBEDDING_DIM):
        seed = (seed * 1103515245 + 12345) & 0xFFFFFFFF
        vec.append((seed / 0xFFFFFFFF) * 2 - 1)
    return vec


class _RetryTestState(TypedDict, total=False):
    attempts: int


def _node_retry_policy_actually_retries() -> bool:
    """Proves LangGraph's own node-level RetryPolicy retries a transiently-failing node
    without any of our code catching/retrying it - the node raises ConnectionError
    (langgraph's default retry_on predicate treats this as transient) twice, and only
    succeeds on the 3rd attempt that LangGraph itself makes.
    """
    counter = {"n": 0}

    def flaky(_state: _RetryTestState) -> dict:
        counter["n"] += 1
        if counter["n"] < 3:
            raise ConnectionError(f"simulated transient network failure {counter['n']}")
        return {"attempts": counter["n"]}

    graph = StateGraph(_RetryTestState)
    graph.add_node("flaky", flaky, retry_policy=NodeRetryPolicy(max_attempts=5, initial_interval=0.01, backoff_factor=1.5))
    graph.add_edge(START, "flaky")
    graph.add_edge("flaky", END)
    compiled = graph.compile(checkpointer=InMemorySaver())

    result = compiled.invoke({}, config={"configurable": {"thread_id": "retry-test"}})
    return result.get("attempts") == 3 and counter["n"] == 3


def _approval_state_survives_restart() -> bool:
    """Proves ApprovalPolicyEngine persistence survives a real process restart: one
    subprocess sets a policy + emergency stop and exits, a second, independent
    subprocess reconnects to the same on-disk SQLite file and reads them back.
    """
    db_path = str(Path(get_settings().afos_db_path).resolve())

    write_script = (
        "import sqlite3;"
        "from core.approval.engine import ApprovalPolicyEngine, ApprovalPolicy;"
        f"conn = sqlite3.connect(r'{db_path}');"
        "engine = ApprovalPolicyEngine(conn);"
        "engine.set_policy('venture-persist-check', 'deploy', ApprovalPolicy.ALWAYS_APPROVE, threshold_usd=5.0);"
        "engine.emergency_stop('venture-persist-check-stopped');"
    )
    subprocess.run([sys.executable, "-c", write_script], check=True, cwd=PROJECT_ROOT)

    read_script = (
        "import sqlite3, sys;"
        "from core.approval.engine import ApprovalPolicyEngine;"
        f"conn = sqlite3.connect(r'{db_path}');"
        "engine = ApprovalPolicyEngine(conn);"
        "policy = engine.get_policy('venture-persist-check', 'deploy');"
        "ok = (policy is not None and policy['policy'].value == 'always_approve'"
        " and engine.is_emergency_stopped('venture-persist-check-stopped'));"
        "sys.exit(0 if ok else 1)"
    )
    result = subprocess.run([sys.executable, "-c", read_script], cwd=PROJECT_ROOT)
    return result.returncode == 0


def main() -> None:
    get_budget_manager()  # subscribe to llm.invoked/tool.invoked before anything else runs

    print("\n== 1. Event Bus ==")
    received = []
    get_event_bus().subscribe("smoke.test", received.append)
    get_event_bus().publish(AFOSEvent(type="smoke.test", source_agent="smoke_test", payload={"ok": True}))
    check("event delivered to subscriber", len(received) == 1 and received[0].payload["ok"] is True)

    print("\n== 2. Agent Registry ==")
    manifest = get_agent_registry().get("manager")
    check("manager manifest registered", manifest.name == "manager" and manifest.lifecycle == "active")

    print("\n== 3. Tool Registry (retry + timeout + cost tracking + schema + permissions) ==")
    calls = {"count": 0}

    class EchoArgs(BaseModel):
        text: str

    def flaky_echo(text: str) -> str:
        calls["count"] += 1
        if calls["count"] < 2:
            raise RuntimeError("simulated transient failure")
        return f"echo: {text}"

    get_tool_registry().register(
        ToolSpec(
            name="echo",
            input_schema=EchoArgs,
            permissions=["internet_access"],
            retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=0.01),
            cost_per_call_usd=0.001,
        ),
        flaky_echo,
    )
    tool_events = []
    get_event_bus().subscribe("tool.invoked", tool_events.append)

    result = get_tool_registry().invoke("echo", agent_name="manager", text="hello")
    check("tool retried through transient failure", result == "echo: hello" and calls["count"] == 2)
    check("tool.invoked event published", len(tool_events) == 1 and tool_events[0].payload["success"] is True)

    try:
        get_tool_registry().invoke("echo", agent_name="manager", wrong_field="oops")
        schema_rejected = False
    except ValidationError:
        schema_rejected = True
    check("invalid tool input rejected by schema before execution", schema_rejected)

    get_agent_registry().register(
        get_agent_registry().get("manager").model_copy(update={"name": "no_perms_agent", "permissions": []})
    )
    try:
        get_tool_registry().invoke("echo", agent_name="no_perms_agent", text="hi")
        permission_enforced = False
    except PermissionDeniedError:
        permission_enforced = True
    check("tool call denied for agent missing required permission", permission_enforced)

    try:
        get_tool_registry().invoke("echo", agent_name="unregistered_ghost", text="hi")
        unregistered_denied = False
    except PermissionDeniedError:
        unregistered_denied = True
    check("tool call denied for unregistered agent", unregistered_denied)

    print("\n== 4. Permission Layer (deny-by-default, standalone) ==")
    gate = get_permission_gate()
    check("manager IS allowed internet_access", gate.is_allowed(manifest, Permission.INTERNET_ACCESS))
    check("manager NOT allowed shell_exec", not gate.is_allowed(manifest, Permission.SHELL_EXEC))

    print("\n== 5. Memory Gateway: vector tier ==")
    vs = VectorStore(sqlite3.connect(":memory:"), embed_fn=fake_embed)
    vs.add("Founders in the B2B SaaS space often underprice their MVP.", kind="research_finding")
    vs.add("Competitor X raised a Series A last quarter.", kind="research_finding")
    hits = vs.search("pricing advice for new SaaS founders", top_k=2)
    check("vector search returns results", len(hits) == 2)

    print("\n== 6. Memory Gateway: working + knowledge tiers ==")
    gateway = get_memory_gateway()
    tables = {
        row[0]
        for row in gateway.knowledge_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    check("knowledge schema created", {"kg_entities", "kg_relationships"}.issubset(tables))

    print("\n== 7. Model Router (Ollama routed to the installed qwen3:8b model) ==")
    from core.model_router import get_model_router

    response = get_model_router().chat(
        [{"role": "user", "content": "Reply with exactly the word: pong"}],
        capability="cheap-fast",
        agent_name="manager",
        timeout=120.0,  # qwen3:8b is a "thinking" model - observed ~66s on this machine
    )
    print(f"     model responded via {response.provider}/{response.model}: {response.content!r}")
    check("model router got a real response via a provider", bool(response.content))

    print("\n== 8. LangGraph node-level RetryPolicy ==")
    check("node RetryPolicy retries a transient ConnectionError", _node_retry_policy_actually_retries())

    print("\n== 9. Approval Framework + LangGraph interrupt/resume ==")
    with gateway.working() as checkpointer:
        graph = compile_main_graph(checkpointer)
        config = {"configurable": {"thread_id": VENTURE_ID}}
        initial_state = {
            "venture_id": VENTURE_ID,
            "idea": "An AI co-pilot that takes solo founders from idea to revenue",
            "phase": "idea",
        }

        first = graph.invoke(initial_state, config=config)
        check("graph paused on approval interrupt", "__interrupt__" in first)
        interrupt_payload = first["__interrupt__"][0].value
        check("interrupt carries the approval request", interrupt_payload["action"] == "advance_to_research")

        final = graph.invoke(Command(resume={"approved": True, "reason": "smoke test approval"}), config=config)
        check("graph resumed past the gate", "__interrupt__" not in final)
        check(
            "approval decision recorded in history",
            any(h.get("event") == "approval_decision" and h.get("approved") is True for h in final["history"]),
        )

    print("\n== 10. Approval Framework: policy/emergency-stop persistence across restart ==")
    check("policy + emergency stop survive a real process restart", _approval_state_survives_restart())

    print("\n== 11. Budget Manager ==")
    monthly = get_budget_manager().monthly_spend()
    check("budget ledger recorded the tool cost", monthly >= 0.001)

    print("\nAll Phase 0 checks passed.")


if __name__ == "__main__":
    main()
