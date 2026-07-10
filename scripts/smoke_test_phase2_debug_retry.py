"""Phase 2, Component 6: Debug & Retry Engine.

Verifies:
  - Agent registration
  - Manifest
  - Tool registration
  - Schema validation
  - Permission enforcement
  - Retry behavior (real exponential backoff, using the kernel's RetryPolicy shape
    for configuration)
  - Dependency Injection (fake DebugProvider + fake retry_fn callables)
  - Failure classification (transient / permanent / unknown)
  - Automatic retry (succeeds within budget)
  - Retry budget respected (never retries endlessly)
  - Event publishing (debug_started/retry_started/retry_completed/debug_completed/
    debug_failed)
  - Graceful failure (never crashes)
  - Regression tests (run separately, see the full suite this script is part of)

Run: python scripts/smoke_test_phase2_debug_retry.py
"""

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from pydantic import ValidationError  # noqa: E402

import agents.coding.debug_retry.agent as debug_retry  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import get_agent_registry, get_tool_registry  # noqa: E402
from core.registries.tool_registry import RetryPolicy  # noqa: E402
from tools.debug.debug_providers import RuleBasedDebugProvider  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def main() -> None:
    print("\n== 1. Agent Registration + Manifest ==")
    manifest = get_agent_registry().get("debug_retry_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares shell_exec permission", manifest.permissions == ["shell_exec"])
    check("declares the diagnose_failure tool", manifest.tools == ["diagnose_failure"])
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Tool Registration + Schema Validation ==")
    spec = get_tool_registry().get_spec("diagnose_failure")
    check("diagnose_failure has an input schema", spec.input_schema is not None)
    check("diagnose_failure requires shell_exec permission", "shell_exec" in spec.permissions)
    try:
        get_tool_registry().invoke("diagnose_failure", agent_name="debug_retry_agent", operation="x")
        rejected = False
    except ValidationError:
        rejected = True
    check("missing required 'error' field rejected before execution", rejected)

    print("\n== 3. Permission Enforcement ==")
    manifest_no_perms = manifest.model_copy(update={"name": "no_debug_perms_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("diagnose_failure", agent_name="no_debug_perms_agent", operation="x", error="y")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without shell_exec is denied", denied)

    print("\n== 4. Failure Classification (Dependency Injection via the tool + provider) ==")
    provider = RuleBasedDebugProvider()
    transient = provider.diagnose("git_status", "Connection timeout while contacting remote", {})
    check("classifies a timeout as transient", transient.failure_type == "transient")
    permanent = provider.diagnose("file_read", "refused to touch the frozen kernel path: core/state.py", {})
    check("classifies a safety refusal as permanent", permanent.failure_type == "permanent")
    unknown = provider.diagnose("mystery", "something bizarre happened xyz123", {})
    check("classifies an unrecognized error conservatively as permanent (non-retryable)", unknown.failure_type == "permanent")
    check("unknown classification has low confidence", unknown.confidence < 0.5)

    print("\n== 5. Automatic Retry (transient failure, succeeds within budget) ==")
    calls = {"n": 0}

    def flaky_retry_fn() -> dict:
        calls["n"] += 1
        if calls["n"] < 2:
            return {"event": "git_operation_failed", "error": "timeout"}
        return {"event": "git_operation_completed", "success": True}

    entry = debug_retry.run_debug_retry(
        operation="git_status",
        retry_fn=flaky_retry_fn,
        initial_error="timeout while running git status",
        venture_id="v-test",
        retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=0.01),
    )
    check("automatic retry succeeded", entry["event"] == "debug_completed" and entry["retried"] is True)
    check("succeeded on the second attempt", entry["retry_attempts"] == 2)
    check("retry budget not marked exhausted on success", entry.get("retry_budget_exhausted") is False)

    print("\n== 6. Retry Budget Respected (always-failing transient, never retries endlessly) ==")

    def always_failing_retry_fn() -> dict:
        return {"event": "git_operation_failed", "error": "timeout"}

    exhausted_entry = debug_retry.run_debug_retry(
        operation="git_status",
        retry_fn=always_failing_retry_fn,
        initial_error="timeout while running git status",
        venture_id="v-test",
        retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=0.01),
    )
    check("stopped after exactly max_attempts, budget marked exhausted", exhausted_entry["retry_budget_exhausted"] is True)
    check("attempted exactly the configured max_attempts", exhausted_entry["retry_attempts"] == 3)

    print("\n== 7. Permanent failures are never retried (zero attempts) ==")

    def should_never_be_called() -> dict:
        raise AssertionError("retry_fn must never be called for a permanent failure")

    permanent_entry = debug_retry.run_debug_retry(
        operation="file_read", retry_fn=should_never_be_called, initial_error="permission denied", venture_id="v-test"
    )
    check("permanent failure recorded with zero retry attempts", permanent_entry["retried"] is False and permanent_entry["retry_attempts"] == 0)

    print("\n== 8. Exponential Backoff (real timing, not linear) ==")
    timestamps: list[float] = []

    def timing_retry_fn() -> dict:
        timestamps.append(time.monotonic())
        return {"event": "x_failed", "error": "timeout"}

    debug_retry.run_debug_retry(
        operation="timing_test",
        retry_fn=timing_retry_fn,
        initial_error="timeout",
        venture_id="v-test",
        retry_policy=RetryPolicy(max_attempts=4, backoff_seconds=0.2),
    )
    deltas = [timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)]
    check("delay roughly doubles between attempts (exponential, not linear)", deltas[1] > deltas[0] * 1.5 and deltas[2] > deltas[1] * 1.5)

    print("\n== 9. Graceful Failure Handling (unexpected internal error, never crashes) ==")
    original_func = get_tool_registry()._funcs["diagnose_failure"]

    def broken_diagnose(operation: str, error: str) -> dict:
        raise RuntimeError("diagnosis engine exploded")

    get_tool_registry()._funcs["diagnose_failure"] = broken_diagnose
    try:
        fail_entry = debug_retry.run_debug_retry(
            operation="x", retry_fn=lambda: {"event": "ok"}, initial_error="y", venture_id="v-test"
        )
        check("unexpected internal failure caught, never raised", fail_entry["event"] == "debug_failed")
    finally:
        get_tool_registry()._funcs["diagnose_failure"] = original_func

    print("\n== 10. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))

    calls2 = {"n": 0}

    def flaky_for_events() -> dict:
        calls2["n"] += 1
        if calls2["n"] < 2:
            return {"event": "x_failed", "error": "timeout"}
        return {"event": "x_completed"}

    debug_retry.run_debug_retry(
        operation="event_test", retry_fn=flaky_for_events, initial_error="timeout",
        venture_id="v-test", retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=0.01),
    )
    check("published debug_started", "debug_started" in seen_events)
    check("published retry_started", "retry_started" in seen_events)
    check("published retry_completed", "retry_completed" in seen_events)
    check("published debug_completed", "debug_completed" in seen_events)

    seen_events.clear()
    original_func2 = get_tool_registry()._funcs["diagnose_failure"]
    get_tool_registry()._funcs["diagnose_failure"] = broken_diagnose
    try:
        debug_retry.run_debug_retry(operation="x", retry_fn=lambda: {"event": "ok"}, initial_error="y", venture_id="v-test")
    finally:
        get_tool_registry()._funcs["diagnose_failure"] = original_func2
    check("published debug_failed", "debug_failed" in seen_events)

    print("\n== 11. Manager-callable node shape ==")
    delta = debug_retry.debug_retry_node({"venture_id": "v-test"})
    check("debug_retry_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
    check("debug_retry_node completed", delta["history"][0]["event"] == "debug_completed")

    print("\nAll Phase 2 Component 6 checks passed.")


if __name__ == "__main__":
    main()
