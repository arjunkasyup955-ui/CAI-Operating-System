import logging
import queue
import re
import threading
import time
import uuid
from typing import Any

from langgraph.errors import GraphBubbleUp

from agents.founder.dashboard.agent import run_founder_dashboard
from core.observability import get_default_execution_history, get_pipeline_events

logger = logging.getLogger("afos.backend.dashboard.jobs")

_VALID_DEPTHS = {"standard", "deep"}

# Ordered (label, started_event, completed_event) triples mirroring the real
# LangGraph node order in workflows/founder_dashboard.py: intake ->
# founder_orchestrator -> research -> decision -> mvp -> build -> deployment
# -> growth -> git_status -> aggregate. intake/git_status/aggregate are thin
# bookends with no dedicated event pair and aren't surfaced as their own
# stage. No new instrumentation added anywhere - every event here is already
# published by the unmodified pipeline stages.
_STAGES: list[tuple[str, str, str]] = [
    ("orchestrator", "founder_pipeline_started", "founder_pipeline_completed"),
    ("research", "research_pipeline_stage_started", "research_pipeline_completed"),
    ("decision", "decision_started", "decision_completed"),
    ("mvp", "mvp_planner_started", "mvp_planner_completed"),
    ("build", "ai_builder_started", "ai_builder_completed"),
    ("deployment", "deployment_started", "deployment_completed"),
    ("growth", "growth_started", "growth_completed"),
]
_STAGE_COUNT = len(_STAGES)

_FAILURE_EVENTS = {
    "founder_pipeline_rejected", "decision_failed", "mvp_planner_failed",
    "ai_builder_failed", "deployment_failed", "growth_failed", "dashboard_failed",
}

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_queue: "queue.Queue[str]" = queue.Queue()
_worker_lock = threading.Lock()
_worker_started = False


def _slugify(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or fallback


def infer_current_stage(venture_id: str) -> str:
    """Derives a 'stage N of 7' label purely by reading events the pipeline
    already publishes via core.observability.get_pipeline_events - genuine
    reuse, not a rebuild of progress tracking, per constraint #1.
    """
    events = get_pipeline_events(venture_id=venture_id)
    if not events:
        return "waiting to start"
    seen = {e["type"] for e in events}
    if "dashboard_completed" in seen:
        return "done"
    if "dashboard_failed" in seen or seen & _FAILURE_EVENTS:
        return "failed"

    last_completed_index = -1
    for i, (label, started, completed) in enumerate(_STAGES):
        if started in seen and completed not in seen:
            return f"{label} ({i + 1}/{_STAGE_COUNT})"
        if completed in seen:
            last_completed_index = i

    if last_completed_index == _STAGE_COUNT - 1:
        return "finishing up"
    return "starting"


def _ensure_worker_started() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        thread = threading.Thread(target=_worker_loop, name="afos-idea-job-worker", daemon=True)
        thread.start()
        _worker_started = True


def _worker_loop() -> None:
    """Drains one job at a time, forever. Single worker thread is a hard
    requirement, not a simplicity shortcut: workflows/decision_engine.py
    caches the research result for a run in a single process-wide global
    (set_research_pipeline_invoker) that is only lock-protected at the
    moment it's set, not for the whole decision->growth window it's read
    over - two concurrent runs would silently cross-contaminate each
    other's research data. Serial execution here is what prevents that.
    """
    while True:
        job_id = _queue.get()
        try:
            _run_job(job_id)
        except Exception:
            logger.exception("idea job worker: unexpected error processing job '%s'", job_id)
            with _jobs_lock:
                job = _jobs.get(job_id)
                if job is not None:
                    job["status"] = "error"
                    job["error"] = "internal worker error"
                    job["finished_at"] = time.time()
        finally:
            _queue.task_done()


def _run_job(job_id: str) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
        job["status"] = "running"
        job["started_at"] = time.time()
        idea, venture_id, research_depth = job["idea"], job["venture_id"], job["research_depth"]

    try:
        result = run_founder_dashboard(idea, venture_id, research_depth)
    except GraphBubbleUp as exc:
        # run_founder_dashboard() never raises for a genuine pipeline
        # failure - it re-raises only LangGraph's human-approval interrupt
        # signal. compile_founder_dashboard() runs with no checkpointer
        # here (stateless), so this path is not expected to be reachable
        # today; handled defensively rather than left to crash the worker.
        with _jobs_lock:
            job = _jobs[job_id]
            job["status"] = "error"
            job["error"] = f"pipeline requested human approval, which this endpoint does not support yet: {exc}"
            job["finished_at"] = time.time()
        return

    status = result.get("status", "unknown")
    finished_at = time.time()
    with _jobs_lock:
        job = _jobs[job_id]
        job["status"] = "error" if status == "failed" else "done"
        job["result"] = result
        job["error"] = result.get("error") or None
        job["finished_at"] = finished_at
        started_at = job["started_at"]

    try:
        research_summary = result.get("research_summary") or {}
        get_default_execution_history().record_execution(
            pipeline_name="founder_dashboard",
            venture_id=venture_id,
            status=status,
            execution_time_seconds=finished_at - started_at,
            confidence_score=research_summary.get("confidence_score"),
            metadata={"idea": idea, "source": "dashboard_submission", "job_id": job_id},
        )
    except Exception:
        logger.exception("idea job worker: failed to record execution history for job '%s'", job_id)


def submit_job(idea: str, venture_id: str | None = None, research_depth: str = "standard") -> str:
    idea = (idea or "").strip()
    if not idea:
        raise ValueError("idea must not be empty")
    depth = research_depth if research_depth in _VALID_DEPTHS else "standard"
    job_id = uuid.uuid4().hex[:12]
    vid = (venture_id or "").strip() or f"submission-{job_id}-{_slugify(idea, 'idea')}"

    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "idea": idea,
            "venture_id": vid,
            "research_depth": depth,
            "status": "queued",
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
        }
    _ensure_worker_started()
    _queue.put(job_id)
    return job_id


def _queue_position(job_id: str) -> int:
    with _jobs_lock:
        queued_ids = [
            j["job_id"] for j in sorted(
                (j for j in _jobs.values() if j["status"] == "queued"),
                key=lambda j: j["created_at"],
            )
        ]
    return queued_ids.index(job_id) if job_id in queued_ids else 0


def get_job(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        snapshot = dict(job)

    if snapshot["status"] == "queued":
        snapshot["current_stage"] = "queued"
        snapshot["queue_position"] = _queue_position(job_id)
    elif snapshot["status"] == "running":
        snapshot["current_stage"] = infer_current_stage(snapshot["venture_id"])
        snapshot["queue_position"] = 0
    else:  # done or error
        snapshot["current_stage"] = "done" if snapshot["status"] == "done" else "failed"
        snapshot["queue_position"] = 0
    return snapshot
