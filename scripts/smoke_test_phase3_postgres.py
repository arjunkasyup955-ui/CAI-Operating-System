"""Phase 3, Component 2: PostgreSQL Integration.

Verifies:
  - Registration (agent + 10 tools)
  - Permissions (internet_access, enforced)
  - Schema validation
  - Retry (ToolRegistry's existing RetryPolicy, via a flaky fake provider)
  - Timeout (ToolRegistry's timeout mechanism, via a deliberately slow function)
  - Graceful failure (real provider - no driver installed, no server running - never
    crashes)
  - Fake provider (full CRUD lifecycle, deterministic, in-memory)
  - Event publishing (postgres_started/completed/failed)
  - Transaction rollback (genuine snapshot/restore semantics, not a no-op)
  - Regression of all previous components (run separately, see the full suite this
    script is part of)

Run: python scripts/smoke_test_phase3_postgres.py
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

import agents.database.postgres.agent as pg_agent  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import ToolSpec, get_agent_registry, get_tool_registry  # noqa: E402
from core.registries.tool_registry import RetryPolicy  # noqa: E402
from tools.database.postgres_providers import (  # noqa: E402
    FakePostgresProvider,
    PostgresHealthStatus,
    PsycopgPostgresProvider,
    set_postgres_provider,
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

    def health_check(self) -> PostgresHealthStatus:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("transient failure")
        return PostgresHealthStatus(healthy=True, host="fake", port=0, version="ok")


def main() -> None:
    print("\n== 1. Registration (agent + 10 tools) ==")
    manifest = get_agent_registry().get("postgres_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares internet_access permission", manifest.permissions == ["internet_access"])
    expected_tools = {
        "postgres_health_check", "postgres_create_database", "postgres_create_table", "postgres_select",
        "postgres_insert", "postgres_update", "postgres_delete", "postgres_begin_transaction",
        "postgres_commit_transaction", "postgres_rollback_transaction",
    }
    check("declares all 10 tools", set(manifest.tools) == expected_tools)
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Permissions ==")
    for tool_name in expected_tools:
        spec = get_tool_registry().get_spec(tool_name)
        check(f"{tool_name} requires internet_access", "internet_access" in spec.permissions)
    manifest_no_perms = manifest.model_copy(update={"name": "no_net_pg_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("postgres_health_check", agent_name="no_net_pg_agent")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without internet_access is denied", denied)

    print("\n== 3. Schema Validation ==")
    try:
        get_tool_registry().invoke("postgres_insert", agent_name="postgres_agent", table_name="x")
        rejected = False
    except ValidationError:
        rejected = True
    check("missing required 'values' field rejected before execution", rejected)
    try:
        get_tool_registry().invoke("postgres_update", agent_name="postgres_agent", table_name="x", where={}, values={"a": 1})
        rejected_empty_where = False
    except ValidationError:
        rejected_empty_where = True
    check("empty 'where' on update rejected (min_length=1)", rejected_empty_where)

    print("\n== 4. Retry Behaviour (ToolRegistry's existing RetryPolicy) ==")
    flaky = _FlakyProvider(fail_times=1)
    set_postgres_provider(flaky)
    try:
        retry_entry = pg_agent.run_postgres_operation("postgres_health_check", venture_id="v-test")
        check("retried past one transient failure and completed", retry_entry["event"] == "postgres_completed")
        check("exactly 2 attempts were made", flaky.calls == 2)
    finally:
        set_postgres_provider(PsycopgPostgresProvider())

    print("\n== 5. Timeout (ToolRegistry's timeout mechanism) ==")
    def _slow_query():
        time.sleep(2.0)
        return {"success": True}

    get_tool_registry().register(
        ToolSpec(
            name="postgres_test_slow_query",
            input_schema=create_model("SlowQueryArgs"),
            permissions=["internet_access"],
            retry_policy=RetryPolicy(max_attempts=1),
            timeout_seconds=0.3,
        ),
        _slow_query,
    )
    start = time.monotonic()
    try:
        get_tool_registry().invoke("postgres_test_slow_query", agent_name="postgres_agent")
        timed_out = False
    except TimeoutError:
        timed_out = True
    elapsed = time.monotonic() - start
    check("a slow query times out per the tool's configured timeout_seconds", timed_out)
    check("did not wait for the full 2s operation to finish", elapsed < 1.5)

    print("\n== 6. Graceful Failure (real provider - no driver installed, no server running) ==")
    real_health = pg_agent.run_postgres_operation("postgres_health_check", venture_id="v-test")
    check("real provider health check completes without crashing", real_health["event"] == "postgres_completed")
    check("reports unhealthy (no driver/server in this sandbox)", real_health["healthy"] is False)
    print(f"     real provider error (expected, no driver installed): {real_health.get('error')}")

    print("\n== 7. Fake Provider: full CRUD lifecycle (deterministic, in-memory) ==")
    set_postgres_provider(FakePostgresProvider())
    try:
        ct = pg_agent.run_postgres_operation("postgres_create_table", venture_id="v-test", table_name="users", columns={"id": "INTEGER", "name": "TEXT"})
        check("create_table succeeded", ct["success"])

        ins = pg_agent.run_postgres_operation("postgres_insert", venture_id="v-test", table_name="users", values={"id": 1, "name": "Alice"})
        check("insert affected exactly 1 row", ins["row_count"] == 1)

        sel = pg_agent.run_postgres_operation("postgres_select", venture_id="v-test", table_name="users")
        check("select returns the inserted row", sel["row_count"] == 1 and sel["rows"][0]["name"] == "Alice")

        upd = pg_agent.run_postgres_operation("postgres_update", venture_id="v-test", table_name="users", where={"id": 1}, values={"name": "Alicia"})
        check("update affected exactly 1 row", upd["row_count"] == 1)

        sel2 = pg_agent.run_postgres_operation("postgres_select", venture_id="v-test", table_name="users", where={"id": 1})
        check("select reflects the update", sel2["rows"][0]["name"] == "Alicia")

        print("\n== 7b. Delete (high risk) genuinely pauses for approval inside a real graph ==")

        class _S(TypedDict, total=False):
            history: Annotated[list, operator.add]

        def delete_node(state: dict) -> dict:
            entry = pg_agent.run_postgres_operation("postgres_delete", venture_id="v-test", table_name="users", where={"id": 1})
            return {"history": [entry]}

        graph = StateGraph(_S)
        graph.add_node("delete", delete_node)
        graph.add_edge(START, "delete")
        graph.add_edge("delete", END)
        compiled = graph.compile(checkpointer=InMemorySaver())

        approve_thread = f"pg-delete-approve-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": approve_thread}}
        first = compiled.invoke({"history": []}, config=config)
        check("delete paused for human approval (real interrupt)", "__interrupt__" in first)
        check("interrupt carries the postgres_delete action", first["__interrupt__"][0].value["action"] == "postgres_delete")

        final = compiled.invoke(Command(resume={"approved": True, "reason": "reviewed"}), config=config)
        check("resumed past the gate after approval", "__interrupt__" not in final)
        check("delete executed after approval", final["history"][0]["event"] == "postgres_completed" and final["history"][0]["row_count"] == 1)

        sel3 = pg_agent.run_postgres_operation("postgres_select", venture_id="v-test", table_name="users")
        check("row genuinely deleted after approval", sel3["row_count"] == 0)

        reject_thread = f"pg-delete-reject-{uuid.uuid4().hex[:8]}"
        pg_agent.run_postgres_operation("postgres_insert", venture_id="v-test", table_name="users", values={"id": 2, "name": "Bob"})
        config2 = {"configurable": {"thread_id": reject_thread}}
        compiled.invoke({"history": []}, config=config2)
        final2 = compiled.invoke(Command(resume={"approved": False, "reason": "not reviewed"}), config=config2)
        check("rejected delete recorded as failed/denied, never executed", final2["history"][0]["event"] == "postgres_failed")
    finally:
        set_postgres_provider(PsycopgPostgresProvider())

    print("\n== 8. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    pg_agent.run_postgres_operation("postgres_health_check", venture_id="v-test")
    check("published postgres_started", "postgres_started" in seen_events)
    check("published postgres_completed", "postgres_completed" in seen_events)

    seen_events.clear()
    set_postgres_provider(_FlakyProvider(fail_times=10))
    try:
        pg_agent.run_postgres_operation("postgres_health_check", venture_id="v-test")
    finally:
        set_postgres_provider(PsycopgPostgresProvider())
    check("published postgres_failed", "postgres_failed" in seen_events)

    print("\n== 9. Transaction Rollback (genuine snapshot/restore, not a no-op) ==")
    set_postgres_provider(FakePostgresProvider())
    try:
        pg_agent.run_postgres_operation("postgres_create_table", venture_id="v-test", table_name="accounts", columns={"id": "INTEGER", "balance": "INTEGER"})
        pg_agent.run_postgres_operation("postgres_insert", venture_id="v-test", table_name="accounts", values={"id": 1, "balance": 100})

        tx = pg_agent.run_postgres_operation("postgres_begin_transaction", venture_id="v-test")
        check("begin_transaction returns a tx_id", bool(tx.get("tx_id")))

        pg_agent.run_postgres_operation("postgres_update", venture_id="v-test", table_name="accounts", where={"id": 1}, values={"balance": 999})
        mid = pg_agent.run_postgres_operation("postgres_select", venture_id="v-test", table_name="accounts", where={"id": 1})
        check("balance reflects the in-transaction update", mid["rows"][0]["balance"] == 999)

        rb = pg_agent.run_postgres_operation("postgres_rollback_transaction", venture_id="v-test", tx_id=tx["tx_id"])
        check("rollback reports success", rb["rolled_back"] is True)

        after = pg_agent.run_postgres_operation("postgres_select", venture_id="v-test", table_name="accounts", where={"id": 1})
        check("balance genuinely restored to its pre-transaction value after rollback", after["rows"][0]["balance"] == 100)

        tx2 = pg_agent.run_postgres_operation("postgres_begin_transaction", venture_id="v-test")
        pg_agent.run_postgres_operation("postgres_update", venture_id="v-test", table_name="accounts", where={"id": 1}, values={"balance": 555})
        pg_agent.run_postgres_operation("postgres_commit_transaction", venture_id="v-test", tx_id=tx2["tx_id"])
        after_commit = pg_agent.run_postgres_operation("postgres_select", venture_id="v-test", table_name="accounts", where={"id": 1})
        check("committed transaction changes persist (not rolled back)", after_commit["rows"][0]["balance"] == 555)
    finally:
        set_postgres_provider(PsycopgPostgresProvider())

    print("\n== 10. Manager-callable node shape ==")
    delta = pg_agent.postgres_agent_node({"venture_id": "v-test"})
    check("postgres_agent_node returns a history delta", "history" in delta and len(delta["history"]) == 1)

    print("\nAll Phase 3 Component 2 checks passed.")


if __name__ == "__main__":
    main()
