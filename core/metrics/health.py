from typing import Any

_HEALTHY_THRESHOLD = 0.8
_DEGRADED_THRESHOLD = 0.5


def _status_from_ratio(ratio: float) -> str:
    if ratio >= _HEALTHY_THRESHOLD:
        return "healthy"
    if ratio >= _DEGRADED_THRESHOLD:
        return "degraded"
    return "critical"


def health_to_score(status: str) -> float:
    return {"healthy": 1.0, "degraded": 0.5, "critical": 0.0, "unknown": 0.5}.get(status, 0.5)


def compute_health(metrics: dict[str, Any], founder_dashboard_report: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deterministic, pure-Python health scoring across the 6 required
    dimensions. Pipeline/Scheduler/Research health are derived from
    core.metrics.collect_metrics()'s own output (real cache/scheduler/
    execution-history data). Builder/Deployment health are "unknown" unless
    the caller optionally passes a Phase 4 Founder Dashboard report dict (a
    plain read of that report's own status fields - never a call into Phase 4
    code, and Phase 4's dashboard module itself is never imported here).
    """
    execution = metrics.get("execution", {})
    scheduler = metrics.get("scheduler", {})
    cache = metrics.get("cache", {})

    total_exec = execution.get("total_executions", 0)
    completed = execution.get("completed", 0)
    pipeline_ratio = (completed / total_exec) if total_exec else 1.0
    pipeline_health = _status_from_ratio(pipeline_ratio)

    by_status = scheduler.get("by_status", {})
    total_jobs = scheduler.get("total_jobs", 0)
    failed_jobs = by_status.get("failed", 0)
    scheduler_ratio = (1.0 - failed_jobs / total_jobs) if total_jobs else 1.0
    scheduler_health = _status_from_ratio(scheduler_ratio)

    avg_confidence = execution.get("average_confidence_score", 0.0)
    research_health = _status_from_ratio(avg_confidence)

    builder_health = "unknown"
    deployment_health = "unknown"
    if founder_dashboard_report:
        build_status = (founder_dashboard_report.get("build_status") or {}).get("status")
        deployment_status = (founder_dashboard_report.get("deployment_status") or {}).get("status")
        if build_status:
            builder_health = "healthy" if build_status in ("completed", "success") else "degraded"
        if deployment_status:
            deployment_health = "healthy" if deployment_status in ("completed", "success") else "degraded"

    dimension_ratios = {"pipeline": pipeline_ratio, "scheduler": scheduler_ratio, "research": avg_confidence}
    overall_score = round(sum(dimension_ratios.values()) / len(dimension_ratios), 4)
    overall_health = _status_from_ratio(overall_score)

    return {
        "overall_health": overall_health,
        "overall_score": overall_score,
        "pipeline_health": pipeline_health,
        "scheduler_health": scheduler_health,
        "research_health": research_health,
        "builder_health": builder_health,
        "deployment_health": deployment_health,
        "cache_size": cache.get("cache_size", 0),
    }
