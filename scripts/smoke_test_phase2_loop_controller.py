"""Phase 2, Component 7: Long-running Task / Loop Controller.

Verifies:
  - Agent registration
  - Manifest
  - Tool registration
  - Schema validation
  - Permission enforcement
  - Dependency Injection (fake LoopProvider)
  - Pause / Resume
  - Cancel
  - Retry (a flaky step recovers within its retry budget)
  - Timeout
  - Max iteration protection
  - Progress tracking
  - Event publishing (all 8 named events)
  - Graceful failure
  - Recovery after interruption (real Memory Gateway, simulated process restart)
  - Regression tests (run separately, see the full suite this script is part of)

Run: python scripts/smoke_test_phase2_loop_controller.py
"""

import logging
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from pydantic import ValidationError  # noqa: E402

import agents.coding.loop_controller.agent as loop_agent  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import get_agent_registry, get_tool_registry  # noqa: E402
from tools.loop.loop_providers import EchoLoopProvider, LoopStepResult, set_loop_provider  # noqa: E402

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


class _FlakyStepProvider:
    name = "flaky"

    def __init__(self, fail_times: int = 2) -> None:
        self.calls = 0
        self._fail_times = fail_times

    def execute_step(self, step: dict, step_index: int) -> LoopStepResult:
        self.calls += 1
        if self.calls <= self._fail_times:
            return LoopStepResult(success=False, step_index=step_index, error="transient step failure")
        return LoopStepResult(success=True, step_index=step_index, output="ok after retries")


class _AlwaysFailingStepProvider:
    name = "always_failing"

    def execute_step(self, step: dict, step_index: int) -> LoopStepResult:
        return LoopStepResult(success=False, step_index=step_index, error="permanent-ish step failure")


def main() -> None:
    print("\n== 1. Agent Registration + Manifest ==")
    manifest = get_agent_registry().get("loop_controller_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares shell_exec permission", manifest.permissions == ["shell_exec"])
    check("declares the execute_loop_step tool", manifest.tools == ["execute_loop_step"])
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Tool Registration + Schema Validation ==")
    spec = get_tool_registry().get_spec("execute_loop_step")
    check("execute_loop_step has an input schema", spec.input_schema is not None)
    check("execute_loop_step requires shell_exec permission", "shell_exec" in spec.permissions)
    try:
        get_tool_registry().invoke("execute_loop_step", agent_name="loop_controller_agent", step={"n": 1})
        rejected = False
    except ValidationError:
        rejected = True
    check("missing required 'step_index' rejected before execution", rejected)

    print("\n== 3. Permission Enforcement ==")
    manifest_no_perms = manifest.model_copy(update={"name": "no_loop_perms_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("execute_loop_step", agent_name="no_loop_perms_agent", step={"n": 1}, step_index=0)
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without shell_exec is denied", denied)

    print("\n== 4. Basic multi-step progression + Progress Tracking ==")
    result = loop_agent.start_loop(steps=[{"n": 1}, {"n": 2}, {"n": 3}], venture_id="v-test", max_iterations=10, timeout_seconds=30.0)
    check("start_loop processed exactly the first step", result["status"] == "running" and result["current_step_index"] == 1)
    check("progress recorded for step 0", result["progress"][0]["step_index"] == 0 and result["progress"][0]["success"] is True)
    loop_id = result["loop_id"]

    r2 = loop_agent.step_loop(loop_id, venture_id="v-test")
    r3 = loop_agent.step_loop(loop_id, venture_id="v-test")
    r4 = loop_agent.step_loop(loop_id, venture_id="v-test")
    check("loop completes after all steps processed", r4["status"] == "completed")
    check("step_loop on an already-completed loop no-ops", loop_agent.step_loop(loop_id, venture_id="v-test")["status"] == "completed")

    print("\n== 5. Pause / Resume ==")
    result2 = loop_agent.start_loop(steps=[{"n": 1}, {"n": 2}, {"n": 3}], venture_id="v-test", max_iterations=10, timeout_seconds=30.0)
    loop_id2 = result2["loop_id"]
    paused = loop_agent.pause_loop(loop_id2, venture_id="v-test")
    check("pause_loop sets status to paused", paused["status"] == "paused")
    no_advance = loop_agent.step_loop(loop_id2, venture_id="v-test")
    check("step_loop while paused does not advance", no_advance["status"] == "paused" and no_advance["current_step_index"] == 1)
    resumed = loop_agent.resume_loop(loop_id2, venture_id="v-test")
    check("resume_loop flips to running and processes one more step", resumed["status"] == "running" and resumed["current_step_index"] == 2)

    print("\n== 6. Cancel (graceful stop, permanent) ==")
    result3 = loop_agent.start_loop(steps=[{"n": 1}, {"n": 2}, {"n": 3}], venture_id="v-test", max_iterations=10, timeout_seconds=30.0)
    loop_id3 = result3["loop_id"]
    cancelled = loop_agent.cancel_loop(loop_id3, venture_id="v-test")
    check("cancel_loop sets status to cancelled", cancelled["status"] == "cancelled")
    still_cancelled = loop_agent.step_loop(loop_id3, venture_id="v-test")
    check("a cancelled loop never processes another step", still_cancelled["status"] == "cancelled")

    print("\n== 7. Max Iteration Protection (never runs forever) ==")
    result4 = loop_agent.start_loop(steps=[{"n": i} for i in range(100)], venture_id="v-test", max_iterations=2, timeout_seconds=30.0)
    loop_id4 = result4["loop_id"]
    loop_agent.step_loop(loop_id4, venture_id="v-test")
    r_over = loop_agent.step_loop(loop_id4, venture_id="v-test")
    check("stops with max_iterations_exceeded, never exceeds the configured limit", r_over["status"] == "failed" and "max_iterations" in r_over["error"])

    print("\n== 8. Timeout Protection ==")
    result5 = loop_agent.start_loop(steps=[{"n": i} for i in range(100)], venture_id="v-test", max_iterations=1000, timeout_seconds=0.05)
    loop_id5 = result5["loop_id"]
    time.sleep(0.2)
    r_timeout = loop_agent.step_loop(loop_id5, venture_id="v-test")
    check("stops with timeout_exceeded once the deadline passes", r_timeout["status"] == "failed" and "timeout" in r_timeout["error"])

    print("\n== 9. Dependency Injection + Retry (flaky step recovers within budget) ==")
    flaky = _FlakyStepProvider(fail_times=2)
    set_loop_provider(flaky)
    try:
        result6 = loop_agent.start_loop(steps=[{"n": 1}], venture_id="v-test", max_iterations=10, timeout_seconds=30.0, retry_budget=5)
        loop_id6 = result6["loop_id"]
        check("first attempt retries (does not advance yet)", result6["status"] == "running" and result6["current_step_index"] == 0 and result6["retry_count"] == 1)
        loop_agent.step_loop(loop_id6, venture_id="v-test")
        r_recovered = loop_agent.step_loop(loop_id6, venture_id="v-test")
        check("step recovers and advances after the flaky failures", r_recovered["current_step_index"] == 1)
    finally:
        set_loop_provider(EchoLoopProvider())

    print("\n== 10. Retry Budget Respected (always-failing step, never retries endlessly) ==")
    set_loop_provider(_AlwaysFailingStepProvider())
    try:
        result7 = loop_agent.start_loop(steps=[{"n": 1}], venture_id="v-test", max_iterations=10, timeout_seconds=30.0, retry_budget=2)
        loop_id7 = result7["loop_id"]
        loop_agent.step_loop(loop_id7, venture_id="v-test")
        r_exhausted = loop_agent.step_loop(loop_id7, venture_id="v-test")
        check("stops with retry budget exhausted, never retries endlessly", r_exhausted["status"] == "failed" and "retry budget" in r_exhausted["error"])
    finally:
        set_loop_provider(EchoLoopProvider())

    print("\n== 11. Graceful Failure Handling (provider raises unexpectedly) ==")

    class _RaisingProvider:
        name = "raising"

        def execute_step(self, step, step_index):
            raise RuntimeError("simulated provider crash")

    set_loop_provider(_RaisingProvider())
    try:
        result8 = loop_agent.start_loop(steps=[{"n": 1}], venture_id="v-test", max_iterations=10, timeout_seconds=30.0, retry_budget=0)
        check("provider exception is caught and treated as a failed step, never crashes", result8["status"] in ("running", "failed"))
    finally:
        set_loop_provider(EchoLoopProvider())

    print("\n== 12. Event Publishing (all named events) ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))

    r = loop_agent.start_loop(steps=[{"n": 1}, {"n": 2}], venture_id="v-test", max_iterations=10, timeout_seconds=30.0)
    lid = r["loop_id"]
    loop_agent.pause_loop(lid, venture_id="v-test")
    loop_agent.resume_loop(lid, venture_id="v-test")
    loop_agent.step_loop(lid, venture_id="v-test")

    r_cancel = loop_agent.start_loop(steps=[{"n": 1}], venture_id="v-test", max_iterations=10, timeout_seconds=30.0)
    loop_agent.cancel_loop(r_cancel["loop_id"], venture_id="v-test")

    flaky2 = _FlakyStepProvider(fail_times=1)
    set_loop_provider(flaky2)
    try:
        r_retry = loop_agent.start_loop(steps=[{"n": 1}], venture_id="v-test", max_iterations=10, timeout_seconds=30.0, retry_budget=3)
    finally:
        set_loop_provider(EchoLoopProvider())

    for expected in (
        "loop_started", "loop_progress", "loop_paused", "loop_resumed",
        "loop_retry", "loop_cancelled", "loop_completed",
    ):
        check(f"published {expected}", expected in seen_events)

    seen_events.clear()
    set_loop_provider(_AlwaysFailingStepProvider())
    try:
        r_fail = loop_agent.start_loop(steps=[{"n": 1}], venture_id="v-test", max_iterations=10, timeout_seconds=30.0, retry_budget=0)
    finally:
        set_loop_provider(EchoLoopProvider())
    check("published loop_failed", "loop_failed" in seen_events)

    print("\n== 13. Recovery After Interruption (real Memory Gateway, cross-process) ==")
    recovery_result = loop_agent.start_loop(steps=[{"n": 1}, {"n": 2}, {"n": 3}], venture_id="v-recovery-smoketest", max_iterations=10, timeout_seconds=60.0)
    recovery_loop_id = recovery_result["loop_id"]

    check_script = (
        "import agents.coding.loop_controller.agent as loop_agent;"
        f"status = loop_agent.get_loop_status('{recovery_loop_id}');"
        "print('recovered current_step_index:', status['current_step_index']);"
        "import sys; sys.exit(0 if status['current_step_index'] == 1 else 1)"
    )
    proc = subprocess.run([sys.executable, "-c", check_script], cwd=PROJECT_ROOT)
    check("a fresh process recovers the exact persisted step index", proc.returncode == 0)

    print("\n== 14. Manager-callable node shape ==")
    delta = loop_agent.loop_controller_node({"venture_id": "v-test"})
    check("loop_controller_node returns a history delta", "history" in delta and len(delta["history"]) == 1)

    print("\nAll Phase 2 Component 7 checks passed.")


if __name__ == "__main__":
    main()
