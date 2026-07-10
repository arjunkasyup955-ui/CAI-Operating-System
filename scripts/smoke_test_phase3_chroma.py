"""Phase 3, Component 3: Chroma Integration.

Verifies:
  - Registration (agent + 8 tools)
  - Permissions (internet_access, enforced)
  - Schema validation
  - Retry (ToolRegistry's existing RetryPolicy, via a flaky fake provider)
  - Timeout (ToolRegistry's timeout mechanism, via a deliberately slow function)
  - Graceful failure (real provider - chromadb not installed - never crashes)
  - Fake provider (full collection + document CRUD lifecycle, deterministic, in-memory)
  - Event publishing (chroma_started/completed/failed)
  - Similarity search (ranks the most relevant document first)
  - Metadata filtering (excludes non-matching documents)
  - Delete (high risk) approval interrupt/resume
  - Regression of all previous components (run separately, see the full suite this
    script is part of)

Run: python scripts/smoke_test_phase3_chroma.py
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

import agents.database.chroma.agent as chroma_agent  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import ToolSpec, get_agent_registry, get_tool_registry  # noqa: E402
from core.registries.tool_registry import RetryPolicy  # noqa: E402
from tools.chroma.chroma_providers import (  # noqa: E402
    ChromaDBProvider,
    ChromaHealthStatus,
    FakeChromaProvider,
    set_chroma_provider,
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

    def health_check(self) -> ChromaHealthStatus:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("transient failure")
        return ChromaHealthStatus(healthy=True, backend="fake")


def main() -> None:
    print("\n== 1. Registration (agent + 8 tools) ==")
    manifest = get_agent_registry().get("chroma_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares internet_access permission", manifest.permissions == ["internet_access"])
    expected_tools = {
        "chroma_health_check", "chroma_create_collection", "chroma_delete_collection", "chroma_list_collections",
        "chroma_add_documents", "chroma_update_documents", "chroma_delete_documents", "chroma_similarity_search",
    }
    check("declares all 8 tools", set(manifest.tools) == expected_tools)
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Permissions ==")
    for tool_name in expected_tools:
        spec = get_tool_registry().get_spec(tool_name)
        check(f"{tool_name} requires internet_access", "internet_access" in spec.permissions)
    manifest_no_perms = manifest.model_copy(update={"name": "no_net_chroma_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("chroma_health_check", agent_name="no_net_chroma_agent")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without internet_access is denied", denied)

    print("\n== 3. Schema Validation ==")
    try:
        get_tool_registry().invoke("chroma_add_documents", agent_name="chroma_agent", collection="x", documents=[])
        rejected = False
    except ValidationError:
        rejected = True
    check("empty documents list rejected (min_length=1)", rejected)

    print("\n== 4. Retry Behaviour (ToolRegistry's existing RetryPolicy) ==")
    flaky = _FlakyProvider(fail_times=1)
    set_chroma_provider(flaky)
    try:
        retry_entry = chroma_agent.run_chroma_operation("chroma_health_check", venture_id="v-test")
        check("retried past one transient failure and completed", retry_entry["event"] == "chroma_completed")
        check("exactly 2 attempts were made", flaky.calls == 2)
    finally:
        set_chroma_provider(ChromaDBProvider())

    print("\n== 5. Timeout (ToolRegistry's timeout mechanism) ==")
    def _slow_query():
        time.sleep(2.0)
        return {"success": True}

    get_tool_registry().register(
        ToolSpec(
            name="chroma_test_slow_query",
            input_schema=create_model("SlowQueryArgs"),
            permissions=["internet_access"],
            retry_policy=RetryPolicy(max_attempts=1),
            timeout_seconds=0.3,
        ),
        _slow_query,
    )
    start = time.monotonic()
    try:
        get_tool_registry().invoke("chroma_test_slow_query", agent_name="chroma_agent")
        timed_out = False
    except TimeoutError:
        timed_out = True
    elapsed = time.monotonic() - start
    check("a slow query times out per the tool's configured timeout_seconds", timed_out)
    check("did not wait for the full 2s operation to finish", elapsed < 1.5)

    print("\n== 6. Graceful Failure (real provider - chromadb not installed) ==")
    real_health = chroma_agent.run_chroma_operation("chroma_health_check", venture_id="v-test")
    check("real provider health check completes without crashing", real_health["event"] == "chroma_completed")
    check("reports unhealthy (chromadb not installed in this sandbox)", real_health["healthy"] is False)
    print(f"     real provider error (expected): {real_health.get('error')}")

    print("\n== 7. Fake Provider: full collection + document CRUD lifecycle ==")
    set_chroma_provider(FakeChromaProvider())
    try:
        ct = chroma_agent.run_chroma_operation("chroma_create_collection", venture_id="v-test", collection_name="research_notes")
        check("create_collection succeeded", ct["success"])

        add = chroma_agent.run_chroma_operation(
            "chroma_add_documents",
            venture_id="v-test",
            collection="research_notes",
            documents=[
                {"id": "doc1", "content": "artisanal hot sauce market is growing fast", "metadata": {"source": "browser", "venture": "hotsauce"}},
                {"id": "doc2", "content": "competitor pricing analysis for premium subscriptions", "metadata": {"source": "browser", "venture": "hotsauce"}},
                {"id": "doc3", "content": "unrelated note about quarterly tax filings", "metadata": {"source": "manual", "venture": "other"}},
            ],
        )
        check("added exactly 3 documents", add["count"] == 3)

        print("\n== 8. Similarity Search ==")
        search = chroma_agent.run_chroma_operation(
            "chroma_similarity_search", venture_id="v-test", collection="research_notes", query="hot sauce market growing", top_k=2
        )
        check("returns the requested number of results", len(search["documents"]) == 2)
        check("ranks the most relevant document first", search["documents"][0]["id"] == "doc1")
        check("distances are ordered ascending (closer = more similar)", search["documents"][0]["distance"] <= search["documents"][1]["distance"])

        print("\n== 9. Metadata Filtering ==")
        filtered = chroma_agent.run_chroma_operation(
            "chroma_similarity_search", venture_id="v-test", collection="research_notes", query="pricing", top_k=5, where={"venture": "hotsauce"}
        )
        filtered_ids = {d["id"] for d in filtered["documents"]}
        check("excludes documents not matching the metadata filter", "doc3" not in filtered_ids)
        check("includes all documents matching the metadata filter", filtered_ids == {"doc1", "doc2"})

        upd = chroma_agent.run_chroma_operation(
            "chroma_update_documents", venture_id="v-test", collection="research_notes",
            documents=[{"id": "doc1", "content": "updated content about hot sauce", "metadata": {"source": "browser", "venture": "hotsauce"}}],
        )
        check("update affected exactly 1 document", upd["count"] == 1)

        print("\n== 10. Delete (high risk) genuinely pauses for approval inside a real graph ==")

        class _S(TypedDict, total=False):
            history: Annotated[list, operator.add]

        def delete_node(state: dict) -> dict:
            entry = chroma_agent.run_chroma_operation("chroma_delete_documents", venture_id="v-test", collection="research_notes", ids=["doc3"])
            return {"history": [entry]}

        graph = StateGraph(_S)
        graph.add_node("delete", delete_node)
        graph.add_edge(START, "delete")
        graph.add_edge("delete", END)
        compiled = graph.compile(checkpointer=InMemorySaver())

        approve_thread = f"chroma-delete-approve-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": approve_thread}}
        first = compiled.invoke({"history": []}, config=config)
        check("delete_documents paused for human approval (real interrupt)", "__interrupt__" in first)
        check("interrupt carries the chroma_delete_documents action", first["__interrupt__"][0].value["action"] == "chroma_delete_documents")

        final = compiled.invoke(Command(resume={"approved": True, "reason": "reviewed"}), config=config)
        check("resumed past the gate after approval", "__interrupt__" not in final)
        check("delete executed after approval", final["history"][0]["event"] == "chroma_completed" and final["history"][0]["count"] == 1)

        after_delete = chroma_agent.run_chroma_operation(
            "chroma_similarity_search", venture_id="v-test", collection="research_notes", query="tax filings", top_k=5
        )
        check("deleted document no longer appears in search results", "doc3" not in {d["id"] for d in after_delete["documents"]})

        lst = chroma_agent.run_chroma_operation("chroma_list_collections", venture_id="v-test")
        check("list_collections includes the created collection", "research_notes" in {d["id"] for d in lst["documents"]})
    finally:
        set_chroma_provider(ChromaDBProvider())

    print("\n== 11. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    chroma_agent.run_chroma_operation("chroma_health_check", venture_id="v-test")
    check("published chroma_started", "chroma_started" in seen_events)
    check("published chroma_completed", "chroma_completed" in seen_events)

    seen_events.clear()
    set_chroma_provider(_FlakyProvider(fail_times=10))
    try:
        chroma_agent.run_chroma_operation("chroma_health_check", venture_id="v-test")
    finally:
        set_chroma_provider(ChromaDBProvider())
    check("published chroma_failed", "chroma_failed" in seen_events)

    print("\n== 12. Manager-callable node shape ==")
    delta = chroma_agent.chroma_agent_node({"venture_id": "v-test"})
    check("chroma_agent_node returns a history delta", "history" in delta and len(delta["history"]) == 1)

    print("\nAll Phase 3 Component 3 checks passed.")


if __name__ == "__main__":
    main()
