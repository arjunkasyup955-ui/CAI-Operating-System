import logging
import threading
import time
import uuid
from typing import Any

import workflows.ai_builder as ai_builder
from agents.founder.ai_builder.agent import run_ai_builder
from agents.founder.ai_builder_reliability.agent import (
    FailureCategory,
    ManagedLoopProvider,
    RetryPolicy,
    classify_failure,
    compute_build_health,
    get_build_status,
    get_default_checkpoint_store,
    get_default_reliability_metrics,
    register_active_build,
    unregister_active_build,
)
from core.execution_cache import ExecutionCacheManager, cached_workflow_invoker, get_default_cache_manager
from core.observability import get_default_execution_history
from core.scheduler import JobContext, JobPriority, JobScheduler, register_job

logger = logging.getLogger("afos.workflows.ai_builder_reliability")

# Serializes concurrent calls to run_managed_ai_builder() within this process,
# since installing a ManagedLoopProvider is a process-global mutation of
# workflows.ai_builder's own module state via its documented, already-public
# set_step_executor_factory()/reset_step_executor_factory() seam - the exact
# same pattern workflows/real_research_pipeline.py already established for
# workflows.research_pipeline.set_stage_functions.
_pipeline_lock = threading.Lock()

MANAGED_BUILD_JOB_TYPE = "ai_builder_managed"


def run_managed_ai_builder(
    idea: str,
    venture_id: str,
    research_depth: str = "standard",
    build_run_id: str | None = None,
    step_timeout_seconds: float = 20.0,
    retry_policy: RetryPolicy | None = None,
    max_build_attempts: int = 3,
    outer_backoff_base_seconds: float = 1.0,
    outer_backoff_max_seconds: float = 10.0,
    cancel_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Eliminates the AI Builder Pipeline's own timeout failure mode
    (workflows.ai_builder.build_loop_node hardcodes timeout_seconds=180.0 -
    frozen, unmodified - as WALL-CLOCK time for the *entire* Loop Controller
    run) by wrapping agents.founder.ai_builder.agent.run_ai_builder() (also
    frozen, unmodified) in an outer retry loop: each attempt installs a fresh
    ManagedLoopProvider (via workflows.ai_builder's own documented
    set_step_executor_factory extension point) that skips every
    already-checkpointed step almost instantly, so a fresh 180s budget on
    attempt 2+ is spent almost entirely on whatever didn't finish before -
    "automatic timeout recovery" and "loop recovery" through checkpoint
    resume, not by changing the Loop Controller's own timeout logic (which
    this component cannot and does not touch).

    Passing the same build_run_id on a later call resumes that build - no
    separate `resume` flag is needed, since resumption is exactly what
    checkpoint-based skip-if-already-done naturally provides.
    """
    checkpoint_store = get_default_checkpoint_store()
    metrics = get_default_reliability_metrics()
    build_run_id = build_run_id or str(uuid.uuid4())
    cancel_event = cancel_event or threading.Event()
    pause_event = pause_event or threading.Event()
    retry_policy = retry_policy or RetryPolicy()

    existing = checkpoint_store.get_checkpoint(build_run_id)
    if existing is not None and existing.get("status") == "completed":
        logger.info("ai_builder_reliability: build %s already completed - returning cached checkpoint state", build_run_id)
        return {
            "status": "completed", "idea": idea, "venture_id": venture_id, "build_run_id": build_run_id,
            "outer_attempts": existing.get("outer_attempts", 0), "failure_category": None,
            "execution_time_seconds": 0.0, "timeout_recoveries": 0,
            "build_health": compute_build_health({"status": "completed"}),
        }
    if existing is None:
        checkpoint_store.create_checkpoint(build_run_id, venture_id, idea)
    else:
        checkpoint_store.request_resume(build_run_id)

    start_time = time.monotonic()
    last_report: dict[str, Any] = {}
    last_failure_category: str | None = None

    attempt = 0
    while attempt < max_build_attempts:
        if cancel_event.is_set() or checkpoint_store.is_cancel_requested(build_run_id):
            checkpoint_store.mark_status(build_run_id, "cancelled")
            metrics.increment("builds_cancelled")
            logger.info("ai_builder_reliability: build %s cancelled before outer attempt %d", build_run_id, attempt + 1)
            break

        while pause_event.is_set() or checkpoint_store.is_pause_requested(build_run_id):
            if cancel_event.is_set() or checkpoint_store.is_cancel_requested(build_run_id):
                break
            checkpoint_store.mark_status(build_run_id, "paused")
            time.sleep(0.2)

        if cancel_event.is_set() or checkpoint_store.is_cancel_requested(build_run_id):
            checkpoint_store.mark_status(build_run_id, "cancelled")
            metrics.increment("builds_cancelled")
            logger.info("ai_builder_reliability: build %s cancelled while paused", build_run_id)
            break

        attempt += 1
        checkpoint_store.mark_status(build_run_id, "in_progress")
        checkpoint_store.increment_outer_attempts(build_run_id)
        logger.info("ai_builder_reliability: build %s starting outer attempt %d/%d", build_run_id, attempt, max_build_attempts)

        provider = ManagedLoopProvider(
            venture_id=venture_id, build_run_id=build_run_id, checkpoint_store=checkpoint_store, metrics=metrics,
            step_timeout_seconds=step_timeout_seconds, retry_policy=retry_policy,
            pause_event=pause_event, cancel_event=cancel_event,
        )
        register_active_build(build_run_id, provider)

        with _pipeline_lock:
            ai_builder.set_step_executor_factory(lambda _vid, _p=provider: _p)
            try:
                last_report = run_ai_builder(idea, venture_id, research_depth)
            finally:
                ai_builder.reset_step_executor_factory()
                unregister_active_build(build_run_id)

        status = last_report.get("status")
        if status == "completed":
            checkpoint_store.mark_status(build_run_id, "completed")
            logger.info("ai_builder_reliability: build %s completed on outer attempt %d", build_run_id, attempt)
            break

        error_text = str(last_report.get("error", ""))
        last_failure_category = classify_failure(error_text)

        if last_failure_category == FailureCategory.PAUSED:
            # The Loop Controller exhausted its own (frozen, hardcoded)
            # retry_budget quickly once ManagedLoopProvider started returning
            # instant "paused" failures - that's not a genuine build failure,
            # so it must not consume one of max_build_attempts. Undo the
            # increment above and loop back to the pause-wait block at the
            # top, which blocks (safely - this is outside any tool-registry-
            # timed call) until resume_build()/cancel_build() is called.
            attempt -= 1
            checkpoint_store.mark_status(build_run_id, "paused")
            logger.info("ai_builder_reliability: build %s attempt was pause-induced, not counted - waiting for resume", build_run_id)
            continue

        checkpoint_store.mark_status(build_run_id, "attempt_failed", last_failure_category=last_failure_category)

        if last_failure_category == FailureCategory.TIMEOUT:
            metrics.increment("timeout_recoveries")

        non_retryable = (FailureCategory.PERMANENT, FailureCategory.CANCELLED)
        can_retry = attempt < max_build_attempts and last_failure_category not in non_retryable
        if cancel_event.is_set() or checkpoint_store.is_cancel_requested(build_run_id):
            can_retry = False
        if can_retry:
            metrics.increment("builds_resumed")
            delay = min(outer_backoff_base_seconds * (2 ** (attempt - 1)), outer_backoff_max_seconds)
            logger.warning(
                "ai_builder_reliability: build %s attempt %d failed (%s) - resuming from checkpoint in %.1fs",
                build_run_id, attempt, last_failure_category, delay,
            )
            time.sleep(delay)
            continue

        final_status = "cancelled" if last_failure_category == FailureCategory.CANCELLED else "failed"
        logger.error("ai_builder_reliability: build %s giving up after attempt %d (%s) - final status=%s", build_run_id, attempt, last_failure_category, final_status)
        checkpoint_store.mark_status(build_run_id, final_status, last_failure_category=last_failure_category)
        break

    elapsed = time.monotonic() - start_time
    final_checkpoint = checkpoint_store.get_checkpoint(build_run_id) or {}
    report = {
        **last_report,
        "build_run_id": build_run_id,
        "venture_id": venture_id,
        "outer_attempts": final_checkpoint.get("outer_attempts", 0),
        "failure_category": last_failure_category,
        "execution_time_seconds": round(elapsed, 4),
        "timeout_recoveries": metrics.snapshot().get("timeout_recoveries", 0),
    }
    if final_checkpoint.get("status") == "cancelled":
        report["status"] = "cancelled"
    report["build_health"] = compute_build_health(report)

    get_default_execution_history().record_execution(
        pipeline_name="ai_builder_managed",
        venture_id=venture_id,
        status=report.get("status", "unknown"),
        execution_time_seconds=elapsed,
        confidence_score=None,
        competitor_count=None,
        metadata={
            "build_run_id": build_run_id,
            "outer_attempts": report["outer_attempts"],
            "failure_category": last_failure_category,
            "steps_completed": report.get("steps_completed"),
            "steps_total": report.get("steps_total"),
        },
        execution_id=build_run_id,
    )
    logger.info(
        "ai_builder_reliability: build %s finished status=%s outer_attempts=%d elapsed=%.2fs",
        build_run_id, report.get("status"), report["outer_attempts"], elapsed,
    )
    return report


def get_cached_managed_build_invoker(cache_manager: ExecutionCacheManager | None = None, ttl_seconds: float | None = 3600.0):
    """Wraps run_managed_ai_builder with Phase 5 Component 1's Execution
    Cache (unmodified) - same cache-key derivation, same duplicate-execution
    guard, same hit/miss statistics every other cached workflow invoker in
    AFOS already gets. Adapts run_managed_ai_builder's richer signature down
    to the (idea, venture_id, research_depth) shape cached_workflow_invoker
    expects, the same accommodation workflows/real_research_pipeline.py
    already uses.
    """
    manager = cache_manager or get_default_cache_manager()
    return cached_workflow_invoker(
        "ai_builder_managed", lambda idea, vid, depth: run_managed_ai_builder(idea, vid, depth), manager, ttl_seconds=ttl_seconds,
    )


def _managed_build_job_fn(ctx: JobContext, idea: str, venture_id: str, research_depth: str = "standard", build_run_id: str | None = None) -> dict[str, Any]:
    """The Job function registered for MANAGED_BUILD_JOB_TYPE - see
    submit_managed_build_job(). Bridges JobContext's own cooperative
    cancellation into the managed build's cancel_event via a lightweight
    watcher thread (rather than modifying JobContext/JobScheduler, Phase 5
    Component 2, frozen), and reports "live build progress" by polling
    get_build_status() - the same thread-safe checkpoint read any other
    caller (including a future dashboard panel) could use - on a short
    interval while the build runs.
    """
    build_run_id = build_run_id or str(uuid.uuid4())
    ctx.log(f"managed build starting for idea={idea!r} venture_id={venture_id!r} build_run_id={build_run_id}")
    ctx.set_progress(2.0, "initializing managed build")

    cancel_event = threading.Event()
    stop_poll = threading.Event()

    def _poll() -> None:
        while not stop_poll.is_set():
            if ctx.is_cancelled():
                cancel_event.set()
            status = get_build_status(build_run_id)
            if status:
                completed = len(status.get("completed_steps", {}))
                pct = min(5.0 + completed * 8.0, 95.0)
                ctx.set_progress(pct, f"step {status.get('current_step_index', 0)} ({status.get('status')})")
            stop_poll.wait(0.3)

    poller = threading.Thread(target=_poll, daemon=True, name=f"ai-builder-reliability-poll-{build_run_id[:8]}")
    poller.start()
    try:
        result = run_managed_ai_builder(idea, venture_id, research_depth, build_run_id=build_run_id, cancel_event=cancel_event)
    finally:
        stop_poll.set()

    ctx.set_progress(100.0, f"completed with status={result.get('status')}")
    ctx.log(f"managed build finished with status={result.get('status')}, outer_attempts={result.get('outer_attempts')}")
    return result


def register_managed_build_job() -> None:
    register_job(MANAGED_BUILD_JOB_TYPE, _managed_build_job_fn)


def submit_managed_build_job(
    scheduler: JobScheduler,
    idea: str,
    venture_id: str,
    research_depth: str = "standard",
    build_run_id: str | None = None,
    priority: int = JobPriority.NORMAL,
    max_retries: int = 1,
    retry_delay_seconds: float = 5.0,
) -> str:
    """Submits a managed build as a background job on Phase 5 Component 2's
    scheduler - "support long-running founder tasks" for a workload that can
    legitimately run for many minutes across several outer attempts.
    max_retries here is the SCHEDULER's own job-level retry (a fresh
    JobScheduler attempt on total job failure); run_managed_ai_builder's own
    max_build_attempts is a separate, inner retry layer that already handles
    timeout/loop recovery before ever reporting the job itself as failed.
    """
    register_managed_build_job()
    return scheduler.submit(
        MANAGED_BUILD_JOB_TYPE,
        args=[idea, venture_id, research_depth],
        kwargs={"build_run_id": build_run_id},
        priority=priority,
        max_retries=max_retries,
        retry_delay_seconds=retry_delay_seconds,
    )
