import threading
from typing import Any

from core.execution_cache import ExecutionCacheManager, get_default_cache_manager
from core.observability.execution_history import get_default_execution_history

_lock = threading.RLock()
_observed_scheduler: Any = None


def get_observed_scheduler() -> Any:
    """Returns whatever JobScheduler instance was registered via
    set_observed_scheduler(), or None if the caller's app never registered
    one. core.metrics deliberately owns no scheduler lifecycle of its own -
    Phase 5 Component 2's JobScheduler is created and started by whoever
    actually runs jobs; this is just where the dashboard is told which live
    instance to read stats from.
    """
    with _lock:
        return _observed_scheduler


def set_observed_scheduler(scheduler: Any) -> None:
    global _observed_scheduler
    with _lock:
        _observed_scheduler = scheduler


def reset_observed_scheduler() -> None:
    global _observed_scheduler
    with _lock:
        _observed_scheduler = None


def _scheduler_metrics() -> dict[str, Any]:
    scheduler = get_observed_scheduler()
    if scheduler is None:
        return {
            "registered": False,
            "total_jobs": 0,
            "queue_size": 0,
            "by_status": {},
            "worker_count": 0,
            "paused": False,
            "recurring_spec_count": 0,
        }
    return {"registered": True, **scheduler.stats()}


def _cache_metrics(cache_manager: ExecutionCacheManager | None = None) -> dict[str, Any]:
    manager = cache_manager or get_default_cache_manager()
    return manager.stats()


def _execution_metrics(venture_id: str | None = None) -> dict[str, Any]:
    store = get_default_execution_history()
    executions = store.list_executions(venture_id=venture_id)
    total = len(executions)
    completed = [e for e in executions if e["status"] == "completed"]
    failed = [e for e in executions if e["status"] == "failed"]
    running = [e for e in executions if e["status"] == "running"]
    exec_times = [e["execution_time_seconds"] for e in executions if e.get("execution_time_seconds") is not None]
    confidences = [e["confidence_score"] for e in executions if e.get("confidence_score") is not None]
    competitor_counts = [e["competitor_count"] for e in executions if e.get("competitor_count") is not None]

    pipeline_durations: dict[str, list[float]] = {}
    for entry in executions:
        if entry.get("execution_time_seconds") is not None:
            pipeline_durations.setdefault(entry["pipeline_name"], []).append(entry["execution_time_seconds"])

    return {
        "total_executions": total,
        "completed": len(completed),
        "failed": len(failed),
        "running": len(running),
        "average_execution_time_seconds": round(sum(exec_times) / len(exec_times), 4) if exec_times else 0.0,
        "average_confidence_score": round(sum(confidences) / len(confidences), 4) if confidences else 0.0,
        "average_competitor_count": round(sum(competitor_counts) / len(competitor_counts), 4) if competitor_counts else 0.0,
        "pipeline_average_duration_seconds": {
            name: round(sum(times) / len(times), 4) for name, times in pipeline_durations.items()
        },
    }


def collect_metrics(venture_id: str | None = None, cache_manager: ExecutionCacheManager | None = None) -> dict[str, Any]:
    """Pure aggregation over three already-real sources: Phase 5 Component 1's
    ExecutionCacheManager.stats(), Phase 5 Component 2's JobScheduler.stats()
    (if one has been registered via set_observed_scheduler), and this
    component's own ExecutionHistoryStore. Nothing here recomputes a
    pipeline's own logic - it only reads what those already-verified
    components already track.
    """
    return {
        "cache": _cache_metrics(cache_manager),
        "scheduler": _scheduler_metrics(),
        "execution": _execution_metrics(venture_id),
    }
