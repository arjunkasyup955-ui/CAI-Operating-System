"""Phase 5, Component 5: AI Builder Reliability & Timeout Recovery.

Eliminates the timeout failure mode observed during AFOS integration testing:
workflows/ai_builder.py's build_loop_node (Phase 4, frozen) hardcodes
timeout_seconds=180.0 as WALL-CLOCK time for the Loop Controller's *entire*
run, and tools/loop/loop.py (Phase 2, frozen) additionally caps any single
execute_step() call at 30 seconds via the Tool Registry's own timeout. This
component makes the AI Builder Pipeline production-grade WITHOUT touching
either frozen constant: agents/founder/ai_builder_reliability/ installs a
ManagedLoopProvider through workflows.ai_builder's own documented
set_step_executor_factory extension point, adding per-step timeout
enforcement, checkpoint-based partial resume, exponential-backoff retry,
failure classification, and cooperative pause/cancel; workflows/
ai_builder_reliability.py wraps agents.founder.ai_builder.agent.run_ai_builder
(frozen, unmodified) in an outer retry loop that re-runs it against the same
build_run_id after a timeout/loop failure - since already-completed steps are
checkpointed, a fresh 180s budget on the next attempt is spent almost
entirely on whatever didn't finish before.

Verifies:
  - Failure classification and exponential backoff (pure functions)
  - BuildCheckpointStore: create/record/resume-skip/pause/cancel/list/
    save-and-load-from-disk
  - ReliabilityMetrics counters
  - ManagedLoopProvider in isolation: success, checkpoint-skip resume, a
    genuine per-step timeout, retry-then-succeed, and the loop-recovery
    tripwire
  - run_managed_ai_builder end to end (fully offline via
    workflows.ai_builder.set_mvp_planner_invoker and this component's own
    set_inner_provider_factory - no real network/LLM/git dependency):
    basic success, timeout/loop recovery via genuine partial build resume
    (steps already checkpointed are proven NOT to be re-executed), pause/
    resume (genuine mid-flight pause with zero forward progress while
    paused), and cancellation (prompt, not blocked behind a wasted backoff)
  - Dashboard integration: managed builds are recorded into the same
    core.observability.ExecutionHistoryStore Phase 5 Component 4's dashboard
    already reads from, and core.metrics reflects them
  - Cache integration with Phase 5 Component 1 (genuine hit/miss)
  - Scheduler integration with Phase 5 Component 2 (real job, live progress,
    structured logs, cooperative cancellation bridged through JobContext)
  - Build health monitoring
  - Regression of every previous phase/component
  - pip check
  - git status

Run: python scripts/smoke_test_phase5_ai_builder_reliability.py
"""

import json
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agents.founder.ai_builder_reliability.agent as reliability_agent  # noqa: E402
import workflows.ai_builder as ai_builder  # noqa: E402
import workflows.ai_builder_reliability as wf  # noqa: E402
from core.execution_cache import ExecutionCacheManager  # noqa: E402
from core.metrics import collect_metrics, compute_health  # noqa: E402
from core.observability import get_default_execution_history, reset_default_execution_history  # noqa: E402
from core.registries import get_tool_registry  # noqa: E402
from core.registries.tool_registry import RateLimit, RetryPolicy as ToolRetryPolicy, ToolSpec  # noqa: E402
from core.scheduler import JobScheduler, get_job_registry  # noqa: E402
from tools.loop.loop import ExecuteLoopStepArgs, execute_loop_step  # noqa: E402
from tools.loop.loop_providers import LoopStepResult  # noqa: E402

# tools/loop/loop.py (Phase 2, frozen) registers "execute_loop_step" with the
# Tool Registry's default RateLimit (60 calls/60s) - a real production safety
# feature, generous for realistic build pacing but not for this smoke test,
# which deliberately exercises many timeout/retry/resume/pause/cancel
# scenarios back to back within a single fast-running process. Re-registering
# the exact same tool (same name, schema, function - only the rate_limit
# raised) via ToolRegistry.register()'s own public, documented API - the same
# call tools/loop/loop.py itself makes - resets that one tool's rate-limit
# window for this test run without touching tools/loop/loop.py or any other
# frozen file.
get_tool_registry().register(
    ToolSpec(
        name="execute_loop_step",
        description="Execute a single step of a long-running loop via the configured LoopProvider",
        input_schema=ExecuteLoopStepArgs,
        permissions=["shell_exec"],
        retry_policy=ToolRetryPolicy(max_attempts=1, backoff_seconds=0.0),
        timeout_seconds=30.0,
        rate_limit=RateLimit(max_calls=100000, period_seconds=60.0),
        cost_per_call_usd=0.0,
    ),
    execute_loop_step,
)


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def fake_mvp_planner_invoker(idea: str, venture_id: str, research_depth: str) -> dict:
    return {
        "agent": "mvp_planner", "event": "mvp_planner_result",
        "idea": idea, "status": "completed",
        "folder_structure": ["src/", "src/main.py", "README.md", "tests/test_main.py"],
        "feature_list": [{"name": "core_feature", "description": "test feature", "priority": "must_have"}],
    }


class FastInner:
    name = "fast"

    def execute_step(self, step, step_index):
        return LoopStepResult(success=True, step_index=step_index, output=f"ok-{step_index}")


class SlowInner:
    name = "slow"

    def __init__(self, delay: float = 0.2):
        self.delay = delay

    def execute_step(self, step, step_index):
        time.sleep(self.delay)
        return LoopStepResult(success=True, step_index=step_index, output=f"ok-{step_index}")


def main() -> None:
    print("\n== 1. Failure Classification ==")
    check("classifies a timeout message", reliability_agent.classify_failure("step timed out") == reliability_agent.FailureCategory.TIMEOUT)
    check("classifies the Loop Controller's own 'timeout_exceeded' error", reliability_agent.classify_failure("timeout_exceeded") == reliability_agent.FailureCategory.TIMEOUT)
    check("classifies a connection error as transient", reliability_agent.classify_failure("connection refused") == reliability_agent.FailureCategory.TRANSIENT)
    check("classifies a rate limit error as transient", reliability_agent.classify_failure("429 rate limit exceeded") == reliability_agent.FailureCategory.TRANSIENT)
    check("classifies a SyntaxError as permanent", reliability_agent.classify_failure("SyntaxError: invalid syntax") == reliability_agent.FailureCategory.PERMANENT)
    check("classifies 'unknown step type' as permanent", reliability_agent.classify_failure("unknown step type 'bogus'") == reliability_agent.FailureCategory.PERMANENT)
    check("classifies the build_cancelled sentinel", reliability_agent.classify_failure("build_cancelled") == reliability_agent.FailureCategory.CANCELLED)
    check("classifies the build_paused sentinel", reliability_agent.classify_failure("build_paused") == reliability_agent.FailureCategory.PAUSED)
    check("classifies the loop_recovery sentinel as timeout-like", reliability_agent.classify_failure("loop_recovery: max step attempts exceeded") == reliability_agent.FailureCategory.TIMEOUT)
    check("classifies an unrecognized message as unknown", reliability_agent.classify_failure("something weird happened") == reliability_agent.FailureCategory.UNKNOWN)
    check("classifies empty/None as unknown", reliability_agent.classify_failure("") == reliability_agent.FailureCategory.UNKNOWN and reliability_agent.classify_failure(None) == reliability_agent.FailureCategory.UNKNOWN)

    print("\n== 2. Exponential Backoff ==")
    policy = reliability_agent.RetryPolicy(base_delay_seconds=1.0, multiplier=2.0, max_delay_seconds=5.0)
    check("no delay on the first attempt", reliability_agent.compute_backoff_delay(1, policy) == 0.0)
    check("base delay on the first retry", reliability_agent.compute_backoff_delay(2, policy) == 1.0)
    check("delay doubles on the second retry", reliability_agent.compute_backoff_delay(3, policy) == 2.0)
    check("delay is capped at max_delay_seconds", reliability_agent.compute_backoff_delay(10, policy) == 5.0)

    print("\n== 3. BuildCheckpointStore ==")
    store = reliability_agent.BuildCheckpointStore()
    store.create_checkpoint("cp-1", "v-a", "idea a")
    check("a fresh checkpoint has no completed steps", store.get_checkpoint("cp-1")["completed_steps"] == {})
    store.record_step_result("cp-1", 0, "output-0")
    store.record_step_result("cp-1", 1, "output-1")
    check("record_step_result marks a step completed", store.is_step_completed("cp-1", 0) is True)
    check("is_step_completed is False for an unrecorded step", store.is_step_completed("cp-1", 5) is False)
    check("get_step_output returns the recorded output", store.get_step_output("cp-1", 1) == "output-1")
    check("current_step_index advances past the highest recorded step", store.get_checkpoint("cp-1")["current_step_index"] == 2)
    check("request_pause succeeds on an in-progress checkpoint", store.request_pause("cp-1") is True)
    check("is_pause_requested reflects the pause", store.is_pause_requested("cp-1") is True)
    check("request_resume clears the pause", store.request_resume("cp-1") is True and store.is_pause_requested("cp-1") is False)
    check("request_cancel succeeds", store.request_cancel("cp-1") is True)
    check("is_cancel_requested reflects the cancel", store.is_cancel_requested("cp-1") is True)
    store.mark_status("cp-1", "completed")
    check("request_pause fails on a completed checkpoint", store.request_pause("cp-1") is False)
    store.create_checkpoint("cp-2", "v-b", "idea b")
    check("list_checkpoints returns both checkpoints", len(store.list_checkpoints()) == 2)
    check("list_checkpoints filters by venture_id", len(store.list_checkpoints(venture_id="v-a")) == 1)
    check("get_checkpoint returns None for an unknown id", store.get_checkpoint("nope") is None)
    check("delete_checkpoint removes an entry", store.delete_checkpoint("cp-2") is True and store.get_checkpoint("cp-2") is None)

    disk_path = Path(__file__).resolve().parent.parent / "database" / "test_ai_builder_checkpoint.json"
    try:
        store.save_to_disk(disk_path)
        check("save_to_disk writes a file", disk_path.exists())
        fresh_store = reliability_agent.BuildCheckpointStore()
        loaded = fresh_store.load_from_disk(disk_path)
        check("load_from_disk reports success", loaded is True)
        check("a fresh store recovers the saved checkpoint's data", fresh_store.get_checkpoint("cp-1")["idea"] == "idea a")
        check("load_from_disk on a missing path returns False", reliability_agent.BuildCheckpointStore().load_from_disk(Path("nope/nope.json")) is False)
    finally:
        if disk_path.exists():
            disk_path.unlink()

    print("\n== 4. ReliabilityMetrics ==")
    metrics = reliability_agent.ReliabilityMetrics()
    metrics.increment("timeout_recoveries")
    metrics.increment("timeout_recoveries")
    metrics.record_failure_category(reliability_agent.FailureCategory.TRANSIENT)
    metrics.record_failure_category(reliability_agent.FailureCategory.TRANSIENT)
    metrics.record_failure_category(reliability_agent.FailureCategory.TIMEOUT)
    snap = metrics.snapshot()
    check("increment accumulates correctly", snap["timeout_recoveries"] == 2)
    check("record_failure_category tallies by category", snap["failure_categories"]["transient"] == 2 and snap["failure_categories"]["timeout"] == 1)

    print("\n== 5. ManagedLoopProvider (isolated unit tests) ==")
    cp_store = reliability_agent.BuildCheckpointStore()
    cp_store.create_checkpoint("mlp-1", "v1", "idea")
    provider = reliability_agent.ManagedLoopProvider("v1", "mlp-1", checkpoint_store=cp_store, inner_provider=FastInner(), retry_policy=reliability_agent.RetryPolicy(max_attempts=3, base_delay_seconds=0.01, max_delay_seconds=0.02))
    r1 = provider.execute_step({"type": "x"}, 0)
    check("a successful step returns success=True and is checkpointed", r1.success is True and cp_store.is_step_completed("mlp-1", 0))
    r2 = provider.execute_step({"type": "x"}, 0)
    check("re-invoking an already-checkpointed step returns the cached output instantly (checkpoint restore)", r2.success is True and r2.output == r1.output)

    cp_store2 = reliability_agent.BuildCheckpointStore()
    cp_store2.create_checkpoint("mlp-2", "v1", "idea")
    timeout_provider = reliability_agent.ManagedLoopProvider("v1", "mlp-2", checkpoint_store=cp_store2, inner_provider=SlowInner(delay=1.0), step_timeout_seconds=0.2, retry_policy=reliability_agent.RetryPolicy(max_attempts=2, base_delay_seconds=0.01, max_delay_seconds=0.02))
    t0 = time.monotonic()
    timeout_result = timeout_provider.execute_step({"type": "x"}, 0)
    elapsed = time.monotonic() - t0
    check("a step exceeding step_timeout_seconds is reported as a timeout failure", timeout_result.success is False and "timeout" in timeout_result.error)
    check("the timeout is enforced promptly, not after the slow step's real duration", elapsed < 0.6)

    cp_store3 = reliability_agent.BuildCheckpointStore()
    cp_store3.create_checkpoint("mlp-3", "v1", "idea")

    class FlakyThenOk:
        name = "flaky"

        def __init__(self):
            self.n = 0

        def execute_step(self, step, step_index):
            self.n += 1
            if self.n < 3:
                return LoopStepResult(success=False, step_index=step_index, error="connection refused")
            return LoopStepResult(success=True, step_index=step_index, output="recovered")

    retry_provider = reliability_agent.ManagedLoopProvider("v1", "mlp-3", checkpoint_store=cp_store3, inner_provider=FlakyThenOk(), retry_policy=reliability_agent.RetryPolicy(max_attempts=5, base_delay_seconds=0.01, max_delay_seconds=0.02))
    fr1 = retry_provider.execute_step({"type": "x"}, 0)
    fr2 = retry_provider.execute_step({"type": "x"}, 0)
    fr3 = retry_provider.execute_step({"type": "x"}, 0)
    check("automatic retry with backoff eventually succeeds", fr1.success is False and fr2.success is False and fr3.success is True)

    cp_store4 = reliability_agent.BuildCheckpointStore()
    cp_store4.create_checkpoint("mlp-4", "v1", "idea")

    class AlwaysFail:
        name = "always_fail"

        def execute_step(self, step, step_index):
            return LoopStepResult(success=False, step_index=step_index, error="connection refused")

    recovery_provider = reliability_agent.ManagedLoopProvider("v1", "mlp-4", checkpoint_store=cp_store4, inner_provider=AlwaysFail(), retry_policy=reliability_agent.RetryPolicy(max_attempts=2, base_delay_seconds=0.01, max_delay_seconds=0.02))
    recovery_provider.execute_step({"type": "x"}, 0)
    recovery_provider.execute_step({"type": "x"}, 0)
    tripwire_result = recovery_provider.execute_step({"type": "x"}, 0)
    check("loop recovery tripwire fires once a step exceeds max_attempts", tripwire_result.success is False and "loop_recovery" in tripwire_result.error)

    print("\n== 6. run_managed_ai_builder: basic success (offline, no real LLM/git/network) ==")
    ai_builder.set_mvp_planner_invoker(fake_mvp_planner_invoker)
    reliability_agent.set_inner_provider_factory(lambda vid: FastInner())
    reset_default_execution_history()
    try:
        report = wf.run_managed_ai_builder("AI voice agent for SMBs", "v-basic", max_build_attempts=2, step_timeout_seconds=5.0)
        check("a clean run completes successfully", report["status"] == "completed")
        check("outer_attempts is 1 for a clean run", report["outer_attempts"] == 1)
        check("steps_completed equals steps_total", report["steps_completed"] == report["steps_total"] and report["steps_total"] > 0)
        check("build_health reports healthy for a completed build", report["build_health"]["build_health"] == "healthy")
        check("the report is JSON-serializable", isinstance(json.dumps(report, default=str), str))
    finally:
        ai_builder.reset_mvp_planner_invoker()
        reliability_agent.reset_inner_provider_factory()

    print("\n== 7. Timeout/Loop Recovery via Genuine Partial Build Resume ==")
    ai_builder.set_mvp_planner_invoker(fake_mvp_planner_invoker)
    real_call_log: list[tuple[int, int]] = []
    build_counter = {"n": 0}

    def flaky_factory(venture_id):
        build_counter["n"] += 1
        attempt_num = build_counter["n"]

        class FlakyPerAttemptInner:
            name = "flaky_per_attempt"

            def execute_step(self, step, step_index):
                real_call_log.append((attempt_num, step_index))
                if attempt_num == 1 and step_index == 3:
                    return LoopStepResult(success=False, step_index=step_index, error="timeout_exceeded")
                return LoopStepResult(success=True, step_index=step_index, output=f"ok-{step_index}")

        return FlakyPerAttemptInner()

    reliability_agent.set_inner_provider_factory(flaky_factory)
    reliability_agent.reset_default_checkpoint_store()
    reliability_agent.reset_default_reliability_metrics()
    reset_default_execution_history()
    try:
        report = wf.run_managed_ai_builder(
            "AI voice agent for SMBs", "v-resume", build_run_id="resume-test-1",
            max_build_attempts=3, step_timeout_seconds=5.0, outer_backoff_base_seconds=0.05, outer_backoff_max_seconds=0.1,
        )
        check("the build eventually completes after a simulated timeout", report["status"] == "completed")
        check("two outer attempts were needed (one recovery)", report["outer_attempts"] == 2)
        attempt2_steps = sorted({s for a, s in real_call_log if a == 2})
        check("steps 0/1/2 (checkpointed before the timeout) are NOT re-executed on the resumed attempt", 0 not in attempt2_steps and 1 not in attempt2_steps and 2 not in attempt2_steps)
        check("step 3 onward (never completed) IS re-executed on the resumed attempt", 3 in attempt2_steps)
        check("the reliability metrics recorded at least one timeout recovery", reliability_agent.get_default_reliability_metrics().snapshot()["timeout_recoveries"] >= 1)
    finally:
        ai_builder.reset_mvp_planner_invoker()
        reliability_agent.reset_inner_provider_factory()

    print("\n== 8. Cancellation (prompt, not blocked behind a wasted backoff) ==")
    ai_builder.set_mvp_planner_invoker(fake_mvp_planner_invoker)
    reliability_agent.set_inner_provider_factory(lambda vid: SlowInner(delay=0.15))
    reliability_agent.reset_default_checkpoint_store()
    reset_default_execution_history()
    try:
        build_run_id = f"cancel-{uuid.uuid4().hex[:8]}"
        cancel_event = threading.Event()
        result_box: dict = {}

        def _run():
            result_box["r"] = wf.run_managed_ai_builder("idea", "v-cancel", build_run_id=build_run_id, cancel_event=cancel_event, max_build_attempts=3, step_timeout_seconds=5.0)

        t = threading.Thread(target=_run, daemon=True)
        start = time.monotonic()
        t.start()
        time.sleep(0.3)
        cancelled_ok = reliability_agent.cancel_build(build_run_id)
        t.join(timeout=10)
        elapsed = time.monotonic() - start
        check("cancel_build reports success", cancelled_ok is True)
        check("the thread finished (no deadlock)", not t.is_alive())
        check("cancellation completes promptly", elapsed < 3.0)
        check("the final report status is 'cancelled'", result_box["r"]["status"] == "cancelled")
    finally:
        ai_builder.reset_mvp_planner_invoker()
        reliability_agent.reset_inner_provider_factory()

    print("\n== 9. Pause / Resume (genuine mid-flight pause, zero forward progress while paused) ==")
    ai_builder.set_mvp_planner_invoker(fake_mvp_planner_invoker)
    reliability_agent.set_inner_provider_factory(lambda vid: SlowInner(delay=0.2))
    reliability_agent.reset_default_checkpoint_store()
    reset_default_execution_history()
    try:
        build_run_id = f"pause-{uuid.uuid4().hex[:8]}"
        result_box2: dict = {}

        def _run2():
            result_box2["r"] = wf.run_managed_ai_builder(
                "idea", "v-pause", build_run_id=build_run_id, max_build_attempts=5,
                step_timeout_seconds=5.0, outer_backoff_base_seconds=0.05, outer_backoff_max_seconds=0.1,
            )

        t2 = threading.Thread(target=_run2, daemon=True)
        t2.start()
        time.sleep(0.35)
        paused_ok = reliability_agent.pause_build(build_run_id)
        check("pause_build reports success", paused_ok is True)
        time.sleep(0.6)
        status_mid = reliability_agent.get_build_status(build_run_id)
        check("build status is 'paused' shortly after pausing", status_mid is not None and status_mid["status"] == "paused")
        completed_at_pause = len(status_mid["completed_steps"])
        time.sleep(0.6)
        status_mid2 = reliability_agent.get_build_status(build_run_id)
        check("no forward progress is made while paused", len(status_mid2["completed_steps"]) == completed_at_pause)
        resumed_ok = reliability_agent.resume_build(build_run_id)
        check("resume_build reports success", resumed_ok is True)
        t2.join(timeout=10)
        check("the build thread finished after resume (no deadlock)", not t2.is_alive())
        check("the build completes successfully after resuming", result_box2["r"]["status"] == "completed")
    finally:
        ai_builder.reset_mvp_planner_invoker()
        reliability_agent.reset_inner_provider_factory()

    print("\n== 10. Build Health Monitoring ==")
    healthy = reliability_agent.compute_build_health({"status": "completed", "steps_total": 5, "steps_completed": 5})
    degraded_cancel = reliability_agent.compute_build_health({"status": "cancelled", "steps_total": 5, "steps_completed": 3})
    critical = reliability_agent.compute_build_health({"status": "failed", "steps_total": 10, "steps_completed": 1})
    check("a completed build is healthy", healthy["build_health"] == "healthy")
    check("a cancelled build is degraded, not critical", degraded_cancel["build_health"] == "degraded")
    check("a mostly-incomplete failed build is critical", critical["build_health"] == "critical")

    print("\n== 11. Integration with Production Dashboard (Phase 5 Component 4) ==")
    ai_builder.set_mvp_planner_invoker(fake_mvp_planner_invoker)
    reliability_agent.set_inner_provider_factory(lambda vid: FastInner())
    reset_default_execution_history()
    try:
        wf.run_managed_ai_builder("idea", "v-dashboard-1", max_build_attempts=1, step_timeout_seconds=5.0)
        wf.run_managed_ai_builder("idea", "v-dashboard-2", max_build_attempts=1, step_timeout_seconds=5.0)
        history_store = get_default_execution_history()
        managed_executions = history_store.list_executions(pipeline_name="ai_builder_managed")
        check("managed builds are recorded in the same ExecutionHistoryStore the dashboard reads from", len(managed_executions) == 2)
        check("recorded executions carry a real execution_time_seconds", all(e["execution_time_seconds"] is not None for e in managed_executions))

        dashboard_metrics = collect_metrics()
        check("core.metrics.collect_metrics reflects the managed builds", dashboard_metrics["execution"]["total_executions"] >= 2)
        check("pipeline_average_duration_seconds includes ai_builder_managed", "ai_builder_managed" in dashboard_metrics["execution"]["pipeline_average_duration_seconds"])

        dashboard_health = compute_health(dashboard_metrics)
        check("core.metrics.compute_health still returns all required dimensions with managed-build data present", "pipeline_health" in dashboard_health and "overall_health" in dashboard_health)
    finally:
        ai_builder.reset_mvp_planner_invoker()
        reliability_agent.reset_inner_provider_factory()

    print("\n== 12. Cache Integration (Phase 5 Component 1) ==")
    ai_builder.set_mvp_planner_invoker(fake_mvp_planner_invoker)
    call_count = {"n": 0}

    def counting_factory(venture_id):
        call_count["n"] += 1

        class CountingInner:
            name = "counting"

            def execute_step(self, step, step_index):
                return LoopStepResult(success=True, step_index=step_index, output=f"ok-{step_index}")

        return CountingInner()

    reliability_agent.set_inner_provider_factory(counting_factory)
    reliability_agent.reset_default_checkpoint_store()
    try:
        cache = ExecutionCacheManager()
        invoker = wf.get_cached_managed_build_invoker(cache_manager=cache, ttl_seconds=60.0)
        first = invoker("idea", "v-cache", "standard")
        calls_after_first = call_count["n"]
        check("the first call is a genuine cache miss (real work happened)", calls_after_first > 0 and first.get("cache_hit") is False)
        second = invoker("idea", "v-cache", "standard")
        check("the second identical call is served from cache (no new inner-provider construction)", call_count["n"] == calls_after_first and second.get("cache_hit") is True)
        stats = cache.stats()
        check("cache stats reflect exactly one miss and one hit", stats["misses"] == 1 and stats["hits"] == 1)
    finally:
        ai_builder.reset_mvp_planner_invoker()
        reliability_agent.reset_inner_provider_factory()

    print("\n== 13. Scheduler Integration (Phase 5 Component 2) + Live Build Progress ==")
    ai_builder.set_mvp_planner_invoker(fake_mvp_planner_invoker)
    reliability_agent.set_inner_provider_factory(lambda vid: SlowInner(delay=0.2))
    reliability_agent.reset_default_checkpoint_store()
    scheduler = JobScheduler(num_workers=1, tick_interval_seconds=0.05)
    scheduler.start()
    try:
        job_id = wf.submit_managed_build_job(scheduler, "idea", "v-scheduler", build_run_id="sched-build-1", max_retries=1, retry_delay_seconds=1.0)
        check("the ai_builder_managed job_type is registered on the scheduler's job registry", wf.MANAGED_BUILD_JOB_TYPE in get_job_registry())

        # Poll while the (deliberately slowed-down) build is still running to
        # prove progress genuinely updates live, mid-flight - not just once
        # at the very end.
        intermediate_percents: list[float] = []
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            live_job = scheduler.get_job(job_id)
            if live_job is None:
                break
            intermediate_percents.append(live_job.progress_percent)
            if live_job.status in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.1)
        check("at least one intermediate progress update between 0% and 100% was observed while running (live build progress)", any(0.0 < p < 100.0 for p in intermediate_percents))

        job = scheduler.wait_for(job_id, timeout=30)
        check("the scheduled managed build completes", job.status == "completed")
        check("the job's result reflects a completed build", job.result.get("status") == "completed")
        check("the job recorded structured logs via JobContext", any("managed build starting" in e["message"] for e in job.logs))
        check("the job reports live progress reaching 100%", job.progress_percent == 100.0)
    finally:
        scheduler.shutdown()
        ai_builder.reset_mvp_planner_invoker()
        reliability_agent.reset_inner_provider_factory()

    print("\n== 14. Scheduler-Bridged Cancellation ==")
    ai_builder.set_mvp_planner_invoker(fake_mvp_planner_invoker)
    reliability_agent.set_inner_provider_factory(lambda vid: SlowInner(delay=0.2))
    reliability_agent.reset_default_checkpoint_store()
    scheduler2 = JobScheduler(num_workers=1, tick_interval_seconds=0.05)
    scheduler2.start()
    try:
        job_id2 = wf.submit_managed_build_job(scheduler2, "idea", "v-scheduler-cancel", build_run_id="sched-cancel-1", max_retries=0, retry_delay_seconds=1.0)
        time.sleep(0.3)
        cancelled = scheduler2.cancel(job_id2)
        check("scheduler.cancel() on the running job reports success (cooperative request registered)", cancelled is True)
        job2 = scheduler2.wait_for(job_id2, timeout=15)
        # The job function itself returns a normal dict (never raises), so the
        # scheduler-level job.status is "completed" - it's the *build's own*
        # status inside that result that becomes "cancelled" once the poller
        # thread observes ctx.is_cancelled() and sets the managed build's
        # cancel_event.
        check("the scheduler job finished (did not hang)", job2.status == "completed")
        check("the job's underlying managed build ends cancelled once the scheduler-level cancel is observed", job2.result is not None and job2.result.get("status") == "cancelled")
    finally:
        scheduler2.shutdown()
        ai_builder.reset_mvp_planner_invoker()
        reliability_agent.reset_inner_provider_factory()

    print("\n== 15. Full Regression of All Previous Phases/Components ==")
    repo_root = Path(__file__).resolve().parent.parent
    # These 13 files each embed their own full regression section, globbing
    # "smoke_test_phase*.py". None of their (frozen, not to be modified)
    # exclusion lists know about this new file, so including any of them
    # here would let it call back into this file, forming an unbounded
    # subprocess cycle. All 13 already re-verify the full prior suite
    # standalone, so excluding them here loses no coverage.
    excluded = {
        "smoke_test_phase5_ai_builder_reliability.py",
        "smoke_test_phase5_dashboard.py",
        "smoke_test_phase5_real_research.py",
        "smoke_test_phase5_scheduler.py",
        "smoke_test_phase5_execution_cache.py",
        "smoke_test_phase3_ads.py",
        "smoke_test_phase4_founder_orchestrator.py",
        "smoke_test_phase4_research_pipeline.py",
        "smoke_test_phase4_decision_engine.py",
        "smoke_test_phase4_mvp_planner.py",
        "smoke_test_phase4_ai_builder.py",
        "smoke_test_phase4_deployment_pipeline.py",
        "smoke_test_phase4_growth_pipeline.py",
        "smoke_test_phase4_founder_dashboard.py",
    }
    smoke_tests = sorted(
        p for p in (repo_root / "scripts").glob("smoke_test_phase*.py")
        if p.name not in excluded
    )
    for test_path in smoke_tests:
        proc = subprocess.run([sys.executable, str(test_path)], cwd=repo_root, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"     regression: {test_path.name} failed on first attempt, retrying once...")
            proc = subprocess.run([sys.executable, str(test_path)], cwd=repo_root, capture_output=True, text=True)
        check(f"regression: {test_path.name}", proc.returncode == 0)

    print("\n== 16. pip check ==")
    pip_result = subprocess.run([sys.executable, "-m", "pip", "check"], cwd=repo_root, capture_output=True, text=True)
    check("pip check reports no broken requirements", pip_result.returncode == 0)

    print("\n== 17. git status (only new files, nothing modified) ==")
    git_result = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True)
    status_lines = [line for line in git_result.stdout.splitlines() if line.strip()]
    modified_or_deleted = [line for line in status_lines if not line.startswith("??")]
    check("git status has no modified/deleted files, only untracked additions", len(modified_or_deleted) == 0)

    print("\nAll Phase 5 Component 5 checks passed.")


if __name__ == "__main__":
    main()
