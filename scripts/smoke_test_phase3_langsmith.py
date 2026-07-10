"""Phase 3, Component 9: LangSmith Integration.

Verifies:
  - Registration (agent + 8 tools)
  - Permissions (internet_access, enforced)
  - Schema validation
  - Retry (ToolRegistry's existing RetryPolicy, via a flaky fake provider)
  - Timeout (ToolRegistry's timeout mechanism, via a deliberately slow function)
  - Graceful failure (real provider - no LANGCHAIN_API_KEY configured - never
    crashes, for both health_check and a mutating operation)
  - Fake provider trace lifecycle (create_trace -> run-before-trace-exists fails ->
    create_run -> log_metrics -> metrics-on-missing-run fails -> log_feedback ->
    update_trace -> end_trace -> update/create_run-after-end both fail, all
    deterministic and in-memory)
  - Metrics logging
  - Feedback logging
  - Dataset upload (including reuse of the same dataset_id for a second upload to
    the same dataset name)
  - Event publishing (langsmith_started/completed/failed)
  - Manager node integration
  - Regression of every previous component (run separately, see the full suite this
    script is part of)

Run: python scripts/smoke_test_phase3_langsmith.py
"""

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from pydantic import ValidationError, create_model  # noqa: E402

import agents.infrastructure.langsmith.agent as ls_agent  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import ToolSpec, get_agent_registry, get_tool_registry  # noqa: E402
from core.registries.tool_registry import RetryPolicy  # noqa: E402
from tools.langsmith.langsmith_providers import (  # noqa: E402
    FakeLangSmithProvider,
    LangSmithHealthStatus,
    LangSmithSDKProvider,
    set_langsmith_provider,
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

    def health_check(self) -> LangSmithHealthStatus:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("transient failure")
        return LangSmithHealthStatus(healthy=True, configured=True, project="flaky-project")


def main() -> None:
    expected_tools = {
        "langsmith_health_check", "langsmith_create_trace", "langsmith_update_trace", "langsmith_end_trace",
        "langsmith_create_run", "langsmith_log_metrics", "langsmith_log_feedback", "langsmith_dataset_upload",
    }

    print("\n== 1. Registration (agent + 8 tools) ==")
    manifest = get_agent_registry().get("langsmith_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares internet_access permission", set(manifest.permissions) == {"internet_access"})
    check("declares all 8 tools", set(manifest.tools) == expected_tools)
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Permissions ==")
    for tool_name in expected_tools:
        spec = get_tool_registry().get_spec(tool_name)
        check(f"{tool_name} requires internet_access", "internet_access" in spec.permissions)

    manifest_no_perms = manifest.model_copy(update={"name": "no_net_langsmith_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("langsmith_health_check", agent_name="no_net_langsmith_agent")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without internet_access is denied", denied)

    try:
        get_tool_registry().invoke("langsmith_health_check", agent_name="git_agent")
        denied_other = False
    except PermissionDeniedError:
        denied_other = True
    check("an unrelated registered agent (git_agent) is denied", denied_other)

    print("\n== 3. Schema Validation ==")
    try:
        get_tool_registry().invoke("langsmith_create_trace", agent_name="langsmith_agent")
        rejected = False
    except ValidationError:
        rejected = True
    check("missing required trace_name rejected", rejected)

    try:
        get_tool_registry().invoke("langsmith_log_metrics", agent_name="langsmith_agent", run_id="run-1")
        rejected2 = False
    except ValidationError:
        rejected2 = True
    check("missing required metrics rejected", rejected2)

    print("\n== 4. Retry Behaviour (ToolRegistry's existing RetryPolicy) ==")
    flaky = _FlakyProvider(fail_times=1)
    set_langsmith_provider(flaky)
    try:
        retry_entry = ls_agent.run_langsmith_operation("langsmith_health_check", venture_id="v-test")
        check("retried past one transient failure and completed", retry_entry["event"] == "langsmith_completed")
        check("exactly 2 attempts were made", flaky.calls == 2)
    finally:
        set_langsmith_provider(LangSmithSDKProvider())

    print("\n== 5. Timeout (ToolRegistry's timeout mechanism) ==")
    def _slow_query():
        time.sleep(2.0)
        return {"success": True}

    get_tool_registry().register(
        ToolSpec(
            name="langsmith_test_slow_query",
            input_schema=create_model("SlowQueryArgs"),
            permissions=["internet_access"],
            retry_policy=RetryPolicy(max_attempts=1),
            timeout_seconds=0.3,
        ),
        _slow_query,
    )
    start = time.monotonic()
    try:
        get_tool_registry().invoke("langsmith_test_slow_query", agent_name="langsmith_agent")
        timed_out = False
    except TimeoutError:
        timed_out = True
    elapsed = time.monotonic() - start
    check("a slow query times out per the tool's configured timeout_seconds", timed_out)
    check("did not wait for the full 2s operation to finish", elapsed < 1.5)

    print("\n== 6. Graceful Failure (real provider - no LANGCHAIN_API_KEY configured) ==")
    real_health = ls_agent.run_langsmith_operation("langsmith_health_check", venture_id="v-test")
    check("real provider health check completes without crashing", real_health["event"] == "langsmith_completed")
    check("reports unhealthy (no API key configured in this sandbox)", real_health["healthy"] is False)
    check("reports not configured", real_health["configured"] is False)
    print(f"     real provider error (expected): {real_health.get('error')}")

    real_create = ls_agent.run_langsmith_operation("langsmith_create_trace", venture_id="v-test", trace_name="unconfigured-trace")
    check("a mutating real-provider operation also fails gracefully without an API key", real_create["event"] == "langsmith_failed")
    print(f"     real provider create_trace error (expected): {real_create.get('error')}")

    print("\n== 7. Fake Provider: trace lifecycle ==")
    set_langsmith_provider(FakeLangSmithProvider())
    try:
        trace = ls_agent.run_langsmith_operation("langsmith_create_trace", venture_id="v-test", trace_name="research-run", inputs={"q": "startup ideas"})
        check("create_trace returned a trace_id", bool(trace.get("trace_id")))
        tid = trace["trace_id"]

        bad_run = ls_agent.run_langsmith_operation("langsmith_create_run", venture_id="v-test", trace_id="trace-does-not-exist", run_name="bad")
        check("create_run on a missing trace fails gracefully", bad_run["event"] == "langsmith_failed")

        run = ls_agent.run_langsmith_operation("langsmith_create_run", venture_id="v-test", trace_id=tid, run_name="search-step", run_type="tool")
        check("create_run returned a run_id", bool(run.get("run_id")))
        rid = run["run_id"]

        print("\n== 8. Metrics Logging ==")
        metrics = ls_agent.run_langsmith_operation("langsmith_log_metrics", venture_id="v-test", run_id=rid, metrics={"latency_ms": 120.5, "tokens": 350})
        check("log_metrics succeeded", metrics["success"])

        bad_metrics = ls_agent.run_langsmith_operation("langsmith_log_metrics", venture_id="v-test", run_id="run-does-not-exist", metrics={"x": 1})
        check("log_metrics on a missing run fails gracefully", bad_metrics["event"] == "langsmith_failed")

        print("\n== 9. Feedback Logging ==")
        feedback = ls_agent.run_langsmith_operation("langsmith_log_feedback", venture_id="v-test", run_id=rid, feedback_key="helpfulness", score=0.9, comment="good result")
        check("log_feedback succeeded", feedback["success"])
        check("log_feedback returned a feedback_id", bool(feedback.get("feedback_id")))

        update = ls_agent.run_langsmith_operation("langsmith_update_trace", venture_id="v-test", trace_id=tid, metadata={"stage": "research"})
        check("update_trace succeeded", update["success"])

        end = ls_agent.run_langsmith_operation("langsmith_end_trace", venture_id="v-test", trace_id=tid, outputs={"result": "done"})
        check("end_trace succeeded", end["success"])

        after_end_update = ls_agent.run_langsmith_operation("langsmith_update_trace", venture_id="v-test", trace_id=tid, metadata={"x": 1})
        check("update_trace after end fails gracefully", after_end_update["event"] == "langsmith_failed")

        after_end_run = ls_agent.run_langsmith_operation("langsmith_create_run", venture_id="v-test", trace_id=tid, run_name="late-run")
        check("create_run after end fails gracefully", after_end_run["event"] == "langsmith_failed")

        print("\n== 10. Dataset Upload ==")
        dataset = ls_agent.run_langsmith_operation("langsmith_dataset_upload", venture_id="v-test", dataset_name="research-examples", examples=[{"inputs": {"q": "x"}, "outputs": {"a": "y"}}])
        check("dataset_upload succeeded", dataset["success"])
        check("dataset_upload returned a dataset_id", bool(dataset.get("dataset_id")))

        dataset2 = ls_agent.run_langsmith_operation("langsmith_dataset_upload", venture_id="v-test", dataset_name="research-examples", examples=[{"inputs": {"q": "z"}, "outputs": {"a": "w"}}])
        check("uploading to the same dataset name reuses the same dataset_id", dataset2["dataset_id"] == dataset["dataset_id"])
    finally:
        set_langsmith_provider(LangSmithSDKProvider())

    print("\n== 11. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    ls_agent.run_langsmith_operation("langsmith_health_check", venture_id="v-test")
    check("published langsmith_started", "langsmith_started" in seen_events)
    check("published langsmith_completed", "langsmith_completed" in seen_events)

    seen_events.clear()
    set_langsmith_provider(_FlakyProvider(fail_times=10))
    try:
        ls_agent.run_langsmith_operation("langsmith_health_check", venture_id="v-test")
    finally:
        set_langsmith_provider(LangSmithSDKProvider())
    check("published langsmith_failed", "langsmith_failed" in seen_events)

    print("\n== 12. Manager-callable node shape ==")
    delta = ls_agent.langsmith_agent_node({"venture_id": "v-test"})
    check("langsmith_agent_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
    check("node result reflects the health check operation", delta["history"][0]["operation"] == "langsmith_health_check")

    print("\nAll Phase 3 Component 9 checks passed.")


if __name__ == "__main__":
    main()
