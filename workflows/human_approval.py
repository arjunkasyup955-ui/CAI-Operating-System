import logging
import time
from typing import Any

from agents.founder.human_approval.agent import (
    STAGES,
    ApprovalPolicyType,
    ApprovalStatus,
    ApprovalStore,
    TERMINAL_STATUSES,
    get_default_approval_store,
    resolve_policy_for_stage,
)
from core.execution_cache import ExecutionCacheManager, get_default_cache_manager
from core.scheduler import JobContext, JobPriority, JobScheduler, register_job

logger = logging.getLogger("afos.workflows.human_approval")

APPROVAL_SWEEP_JOB_TYPE = "human_approval_sweep"


def request_stage_approval(
    venture_id: str,
    stage: str,
    payload: dict[str, Any],
    requested_by: str = "system",
    policy_override: dict[str, Any] | None = None,
    store: ApprovalStore | None = None,
) -> dict[str, Any]:
    """Creates (or auto-resolves) an approval request for one of the 6
    founder review stages, using resolve_policy_for_stage()'s default policy
    table unless policy_override supplies its own settings. This is the
    entry point a caller uses to submit a completed stage's own output
    (research report, decision report, MVP plan, build report, deployment
    report, growth report) for founder/reviewer review.
    """
    store = store or get_default_approval_store()
    policy_config = {**resolve_policy_for_stage(stage), **(policy_override or {})}
    policy = policy_config.pop("policy", ApprovalPolicyType.MANUAL)
    return store.create_request(venture_id=venture_id, stage=stage, payload=payload, requested_by=requested_by, policy=policy, **policy_config)


def wait_for_stage_approval(
    approval_id: str,
    timeout_seconds: float | None = None,
    poll_interval: float = 0.05,
    store: ApprovalStore | None = None,
) -> dict[str, Any] | None:
    """Blocks (bounded, if timeout_seconds is given) until the approval
    request reaches a terminal status (approved/rejected/expired/
    auto_approved). Mirrors JobScheduler.wait_for()'s own polling pattern.
    """
    store = store or get_default_approval_store()
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    while True:
        request = store.get_request(approval_id)
        if request is None or request["status"] in TERMINAL_STATUSES:
            return request
        if deadline is not None and time.monotonic() >= deadline:
            return request
        time.sleep(poll_interval)


def gate_stage(
    venture_id: str,
    stage: str,
    payload: dict[str, Any],
    requested_by: str = "system",
    policy_override: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
    poll_interval: float = 0.05,
    store: ApprovalStore | None = None,
) -> tuple[bool, dict[str, Any]]:
    """The Founder Review Workflow's actual gate: request review for `stage`'s
    output, wait (bounded) for a decision, and return (can_proceed, request).
    can_proceed is True only for approved/auto_approved - a caller
    orchestrating a founder journey (Research -> Decision -> MVP -> AI
    Builder -> Deployment -> Growth) calls this once per stage and only
    continues to the next stage when can_proceed is True. Deliberately a
    plain, reusable function rather than a change to any frozen Phase 4
    pipeline's own graph - it's up to whoever orchestrates the founder
    journey to call this between stages.
    """
    if stage not in STAGES:
        raise ValueError(f"unknown approval stage: {stage!r} (expected one of {STAGES})")
    request = request_stage_approval(venture_id, stage, payload, requested_by=requested_by, policy_override=policy_override, store=store)
    if request["status"] in TERMINAL_STATUSES:
        return request["status"] in (ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED), request
    resolved = wait_for_stage_approval(request["approval_id"], timeout_seconds=timeout_seconds, poll_interval=poll_interval, store=store)
    if resolved is None:
        return False, request
    return resolved["status"] in (ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED), resolved


# ========================================================================== #
# Metrics
# ========================================================================== #

def compute_approval_metrics(venture_id: str | None = None, store: ApprovalStore | None = None) -> dict[str, Any]:
    """Pure aggregation over ApprovalStore.list_requests() - approval time,
    reviewer load, pending count, approval/rejection rate, average review
    time. No LLM, fully deterministic.
    """
    store = store or get_default_approval_store()
    requests = store.list_requests(venture_id=venture_id)

    resolved = [r for r in requests if r["resolved_at"] is not None]
    review_times = [r["resolved_at"] - r["created_at"] for r in resolved]
    approved = [r for r in resolved if r["status"] in (ApprovalStatus.APPROVED, ApprovalStatus.AUTO_APPROVED)]
    rejected = [r for r in resolved if r["status"] == ApprovalStatus.REJECTED]
    pending = [r for r in requests if r["status"] == ApprovalStatus.PENDING]

    reviewer_load: dict[str, int] = {}
    for r in pending:
        for reviewer in r["assigned_reviewers"]:
            reviewer_load[reviewer] = reviewer_load.get(reviewer, 0) + 1

    return {
        "total_requests": len(requests),
        "pending_count": len(pending),
        "resolved_count": len(resolved),
        "approved_count": len(approved),
        "rejected_count": len(rejected),
        "approval_rate": round(len(approved) / len(resolved), 4) if resolved else 0.0,
        "rejected_rate": round(len(rejected) / len(resolved), 4) if resolved else 0.0,
        "average_review_time_seconds": round(sum(review_times) / len(review_times), 4) if review_times else 0.0,
        "reviewer_load": reviewer_load,
        "per_request_review_time_seconds": {r["approval_id"]: round(r["resolved_at"] - r["created_at"], 4) for r in resolved},
    }


def compute_reviewer_activity(venture_id: str | None = None, reviewer_id: str | None = None, store: ApprovalStore | None = None) -> list[dict[str, Any]]:
    """Flattens every approval request's decision_history into one
    reviewer-centric activity list ("Reviewer Activity" - who decided what,
    when, on which stage), most recent first.
    """
    store = store or get_default_approval_store()
    requests = store.list_requests(venture_id=venture_id)
    entries: list[dict[str, Any]] = []
    for request in requests:
        for decision in request["decision_history"]:
            if decision["reviewer"] == "system":
                continue
            if reviewer_id is not None and decision["reviewer"] != reviewer_id:
                continue
            entries.append({
                "reviewer": decision["reviewer"], "action": decision["action"], "note": decision["note"],
                "timestamp": decision["timestamp"], "approval_id": request["approval_id"],
                "stage": request["stage"], "venture_id": request["venture_id"],
            })
    entries.sort(key=lambda e: e["timestamp"], reverse=True)
    return entries


def get_cached_approval_metrics(venture_id: str | None = None, cache_manager: ExecutionCacheManager | None = None, ttl_seconds: float = 15.0) -> dict[str, Any]:
    """Wraps compute_approval_metrics with Phase 5 Component 1's Execution
    Cache (unmodified) - a short TTL since approval state changes frequently
    but a dashboard polling every second shouldn't recompute the full
    aggregation every time.
    """
    manager = cache_manager or get_default_cache_manager()
    cache_key = manager.make_cache_key("human_approval_metrics", venture_id=venture_id)
    cached = manager.get(cache_key)
    if cached is not None:
        return {**cached, "cache_hit": True}
    start = time.monotonic()
    result = compute_approval_metrics(venture_id=venture_id)
    elapsed = time.monotonic() - start
    manager.put(cache_key, "human_approval_metrics", result, elapsed, ttl_seconds=ttl_seconds)
    return {**result, "cache_hit": False}


# ========================================================================== #
# Scheduler integration: periodic timeout/expiration/escalation sweep
# ========================================================================== #

def _sweep_job_fn(ctx: JobContext, interval_seconds: float = 60.0, max_iterations: int | None = None) -> dict[str, Any]:
    """A long-running scheduler job that periodically sweeps the default
    ApprovalStore for timed-out requests (expiring or escalating them per
    each request's own escalate_on_timeout setting). max_iterations is None
    for real production use (runs until cancelled); tests pass a small
    number so the job naturally completes.
    """
    store = get_default_approval_store()
    ctx.log(f"approval sweep job starting (interval={interval_seconds}s)")
    iterations = 0
    total_swept = 0
    while max_iterations is None or iterations < max_iterations:
        if ctx.is_cancelled():
            ctx.log("approval sweep job cancelled")
            break
        swept = store.sweep_expirations()
        total_swept += swept
        iterations += 1
        ctx.set_progress(min(5.0 + iterations, 95.0), f"swept {swept} expirations (iteration {iterations})")
        if max_iterations is not None and iterations >= max_iterations:
            break
        time.sleep(interval_seconds)
    ctx.set_progress(100.0, f"sweep finished after {iterations} iterations, {total_swept} total expirations handled")
    return {"status": "completed", "iterations": iterations, "total_swept": total_swept}


def register_approval_sweep_job() -> None:
    register_job(APPROVAL_SWEEP_JOB_TYPE, _sweep_job_fn)


def submit_approval_sweep_job(
    scheduler: JobScheduler,
    interval_seconds: float = 60.0,
    max_iterations: int | None = None,
    priority: int = JobPriority.LOW,
) -> str:
    """Submits the recurring approval-timeout sweep as a background job on
    Phase 5 Component 2's scheduler - "Approval Timeout"/"Approval
    Expiration"/"Escalation Rules" enforced on a cadence rather than only
    lazily on read.
    """
    register_approval_sweep_job()
    return scheduler.submit(
        APPROVAL_SWEEP_JOB_TYPE, kwargs={"interval_seconds": interval_seconds, "max_iterations": max_iterations}, priority=priority,
    )
