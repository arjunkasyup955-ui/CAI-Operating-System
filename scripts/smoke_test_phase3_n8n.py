"""Phase 3, Component 5: n8n Integration.

Verifies:
  - Registration (agent + 10 tools)
  - Permissions (internet_access, enforced)
  - Schema validation
  - Retry (ToolRegistry's existing RetryPolicy, via a flaky fake provider)
  - Timeout (ToolRegistry's timeout mechanism, via a deliberately slow function)
  - Graceful failure (real provider - n8n not reachable locally - never crashes)
  - Fake provider lifecycle (create -> list -> execute-before-active fails ->
    update -> activate -> get -> deactivate, deterministic, in-memory)
  - Approval interrupts (execute_workflow and delete_workflow are high risk and
    genuinely pause inside a real graph; both approve and reject paths)
  - Event publishing (n8n_started/completed/failed)
  - Regression of every previous component (run separately, see the full suite this
    script is part of)

Run: python scripts/smoke_test_phase3_n8n.py
"""

import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Annotated, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

import operator  # noqa: E402

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command  # noqa: E402
from pydantic import ValidationError, create_model  # noqa: E402

import agents.infrastructure.n8n.agent as n8n_agent  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import ToolSpec, get_agent_registry, get_tool_registry  # noqa: E402
from core.registries.tool_registry import RetryPolicy  # noqa: E402
from tools.n8n.n8n_providers import (  # noqa: E402
    FakeN8nProvider,
    N8nHealthStatus,
    N8nRESTProvider,
    set_n8n_provider,
)


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


class _FlakyProvider:
    name = "flaky"

    def __init__(self, fail_times: int = 1) -> None:
        self.calls = 0
        self._fail_times = fail_times

    def health_check(self) -> N8nHealthStatus:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("transient failure")
        return N8nHealthStatus(healthy=True, base_url="flaky")


def main() -> None:
    expected_tools = {
        "n8n_health_check", "n8n_list_workflows", "n8n_get_workflow", "n8n_create_workflow",
        "n8n_update_workflow", "n8n_activate_workflow", "n8n_deactivate_workflow",
        "n8n_delete_workflow", "n8n_execute_workflow", "n8n_workflow_executions",
    }

    print("\n== 1. Registration (agent + 10 tools) ==")
    manifest = get_agent_registry().get("n8n_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares internet_access permission", set(manifest.permissions) == {"internet_access"})
    check("declares all 10 tools", set(manifest.tools) == expected_tools)
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Permissions ==")
    for tool_name in expected_tools:
        spec = get_tool_registry().get_spec(tool_name)
        check(f"{tool_name} requires internet_access", "internet_access" in spec.permissions)

    manifest_no_perms = manifest.model_copy(update={"name": "no_net_n8n_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("n8n_health_check", agent_name="no_net_n8n_agent")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without internet_access is denied", denied)

    try:
        get_tool_registry().invoke("n8n_health_check", agent_name="git_agent")
        denied_other = False
    except PermissionDeniedError:
        denied_other = True
    check("an unrelated registered agent (git_agent) is denied", denied_other)

    print("\n== 3. Schema Validation ==")
    try:
        get_tool_registry().invoke("n8n_get_workflow", agent_name="n8n_agent")
        rejected = False
    except ValidationError:
        rejected = True
    check("missing required workflow_id rejected", rejected)

    print("\n== 4. Retry Behaviour (ToolRegistry's existing RetryPolicy) ==")
    flaky = _FlakyProvider(fail_times=1)
    set_n8n_provider(flaky)
    try:
        retry_entry = n8n_agent.run_n8n_operation("n8n_health_check", venture_id="v-test")
        check("retried past one transient failure and completed", retry_entry["event"] == "n8n_completed")
        check("exactly 2 attempts were made", flaky.calls == 2)
    finally:
        set_n8n_provider(N8nRESTProvider())

    print("\n== 5. Timeout (ToolRegistry's timeout mechanism) ==")
    def _slow_query():
        time.sleep(2.0)
        return {"success": True}

    get_tool_registry().register(
        ToolSpec(
            name="n8n_test_slow_query",
            input_schema=create_model("SlowQueryArgs"),
            permissions=["internet_access"],
            retry_policy=RetryPolicy(max_attempts=1),
            timeout_seconds=0.3,
        ),
        _slow_query,
    )
    start = time.monotonic()
    try:
        get_tool_registry().invoke("n8n_test_slow_query", agent_name="n8n_agent")
        timed_out = False
    except TimeoutError:
        timed_out = True
    elapsed = time.monotonic() - start
    check("a slow query times out per the tool's configured timeout_seconds", timed_out)
    check("did not wait for the full 2s operation to finish", elapsed < 1.5)

    print("\n== 6. Graceful Failure (real provider - n8n not reachable locally) ==")
    real_health = n8n_agent.run_n8n_operation("n8n_health_check", venture_id="v-test")
    check("real provider health check completes without crashing", real_health["event"] == "n8n_completed")
    check("reports unhealthy (no n8n instance running in this sandbox)", real_health["healthy"] is False)
    print(f"     real provider error (expected): {real_health.get('error')}")

    print("\n== 7. Fake Provider: full workflow lifecycle ==")
    set_n8n_provider(FakeN8nProvider())
    try:
        create = n8n_agent.run_n8n_operation(
            "n8n_create_workflow", venture_id="v-test", workflow_name="daily-report", nodes=[{"type": "start"}], connections={},
        )
        check("create_workflow returned a workflow_id", bool(create.get("workflow_id")))
        wid = create["workflow_id"]

        lst = n8n_agent.run_n8n_operation("n8n_list_workflows", venture_id="v-test")
        check("created workflow appears in list_workflows", any(w["id"] == wid for w in lst["workflows"]))
        check("new workflow starts inactive", lst["workflows"][0]["active"] is False)

        upd = n8n_agent.run_n8n_operation(
            "n8n_update_workflow", venture_id="v-test", workflow_id=wid, nodes=[{"type": "start"}, {"type": "end"}],
        )
        check("update_workflow succeeded", upd["success"])

        act = n8n_agent.run_n8n_operation("n8n_activate_workflow", venture_id="v-test", workflow_id=wid)
        check("activate_workflow succeeded", act["success"])

        got = n8n_agent.run_n8n_operation("n8n_get_workflow", venture_id="v-test", workflow_id=wid)
        check("workflow reports active after activation", got["workflows"][0]["active"] is True)

        deact = n8n_agent.run_n8n_operation("n8n_deactivate_workflow", venture_id="v-test", workflow_id=wid)
        check("deactivate_workflow succeeded", deact["success"])
        got2 = n8n_agent.run_n8n_operation("n8n_get_workflow", venture_id="v-test", workflow_id=wid)
        check("workflow reports inactive after deactivation", got2["workflows"][0]["active"] is False)

        print("\n== 8. Approval Interrupts: execute_workflow (high risk) ==")

        class _S(TypedDict, total=False):
            history: Annotated[list, operator.add]

        def execute_node(state: dict) -> dict:
            entry = n8n_agent.run_n8n_operation("n8n_execute_workflow", venture_id="v-test", workflow_id=wid)
            return {"history": [entry]}

        graph = StateGraph(_S)
        graph.add_node("execute", execute_node)
        graph.add_edge(START, "execute")
        graph.add_edge("execute", END)
        compiled = graph.compile(checkpointer=InMemorySaver())

        inactive_thread = f"n8n-execute-inactive-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": inactive_thread}}
        first = compiled.invoke({"history": []}, config=config)
        check("execute_workflow paused for human approval (real interrupt)", "__interrupt__" in first)
        check("interrupt carries the n8n_execute_workflow action", first["__interrupt__"][0].value["action"] == "n8n_execute_workflow")

        final = compiled.invoke(Command(resume={"approved": True, "reason": "reviewed"}), config=config)
        check("resumed past the gate after approval", "__interrupt__" not in final)
        check(
            "fake provider enforces activation-before-execution even when approved",
            final["history"][0]["event"] == "n8n_failed" and "not active" in final["history"][0]["error"],
        )

        n8n_agent.run_n8n_operation("n8n_activate_workflow", venture_id="v-test", workflow_id=wid)

        active_thread = f"n8n-execute-active-{uuid.uuid4().hex[:8]}"
        config_active = {"configurable": {"thread_id": active_thread}}
        compiled.invoke({"history": []}, config=config_active)
        final_active = compiled.invoke(Command(resume={"approved": True}), config=config_active)
        check("execute_workflow succeeds once active and approved", final_active["history"][0]["event"] == "n8n_completed")

        execs = n8n_agent.run_n8n_operation("n8n_workflow_executions", venture_id="v-test", workflow_id=wid)
        check("workflow_executions reflects the successful execution", len(execs["executions"]) == 1)

        print("\n== 9. Approval Interrupts: delete_workflow (high risk, reject + approve) ==")

        def delete_node(state: dict) -> dict:
            entry = n8n_agent.run_n8n_operation("n8n_delete_workflow", venture_id="v-test", workflow_id=wid)
            return {"history": [entry]}

        graph2 = StateGraph(_S)
        graph2.add_node("delete", delete_node)
        graph2.add_edge(START, "delete")
        graph2.add_edge("delete", END)
        compiled2 = graph2.compile(checkpointer=InMemorySaver())

        reject_thread = f"n8n-delete-reject-{uuid.uuid4().hex[:8]}"
        config_reject = {"configurable": {"thread_id": reject_thread}}
        first_reject = compiled2.invoke({"history": []}, config=config_reject)
        check("delete_workflow paused for human approval (real interrupt)", "__interrupt__" in first_reject)

        final_reject = compiled2.invoke(Command(resume={"approved": False, "reason": "not now"}), config=config_reject)
        check("rejected delete does not execute", final_reject["history"][0]["event"] == "n8n_failed")
        still_there = n8n_agent.run_n8n_operation("n8n_get_workflow", venture_id="v-test", workflow_id=wid)
        check("workflow still exists after rejected delete", still_there["event"] == "n8n_completed")

        approve_thread = f"n8n-delete-approve-{uuid.uuid4().hex[:8]}"
        config_approve = {"configurable": {"thread_id": approve_thread}}
        compiled2.invoke({"history": []}, config=config_approve)
        final_approve = compiled2.invoke(Command(resume={"approved": True, "reason": "reviewed"}), config=config_approve)
        check("approved delete executes", final_approve["history"][0]["event"] == "n8n_completed")

        gone = n8n_agent.run_n8n_operation("n8n_get_workflow", venture_id="v-test", workflow_id=wid)
        check("workflow genuinely removed after approval", gone["event"] == "n8n_failed")
    finally:
        set_n8n_provider(N8nRESTProvider())

    print("\n== 10. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    n8n_agent.run_n8n_operation("n8n_health_check", venture_id="v-test")
    check("published n8n_started", "n8n_started" in seen_events)
    check("published n8n_completed", "n8n_completed" in seen_events)

    seen_events.clear()
    set_n8n_provider(_FlakyProvider(fail_times=10))
    try:
        n8n_agent.run_n8n_operation("n8n_health_check", venture_id="v-test")
    finally:
        set_n8n_provider(N8nRESTProvider())
    check("published n8n_failed", "n8n_failed" in seen_events)

    print("\n== 11. Manager-callable node shape ==")
    delta = n8n_agent.n8n_agent_node({"venture_id": "v-test"})
    check("n8n_agent_node returns a history delta", "history" in delta and len(delta["history"]) == 1)

    print("\nAll Phase 3 Component 5 checks passed.")


if __name__ == "__main__":
    main()
