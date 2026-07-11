from typing import Any

from core.metrics.health import health_to_score

CHART_NAMES = (
    "pipeline_timeline",
    "execution_time",
    "cache_hit_rate",
    "research_confidence",
    "competitor_count",
    "job_queue",
    "health_score",
    "overall_score",
)

_HEALTH_DIMENSIONS = ("pipeline_health", "scheduler_health", "research_health", "builder_health", "deployment_health")


def build_chart_data(metrics: dict[str, Any], history: list[dict[str, Any]], health: dict[str, Any], max_points: int = 20) -> dict[str, Any]:
    """Produces Chart.js-ready {type, labels, datasets} payloads for all 8
    required charts, built entirely from real data already collected by
    core.metrics.collect_metrics()/compute_health() and
    core.observability.execution_history - no fabricated numbers.
    """
    recent = list(reversed(history))[-max_points:]
    labels = [e["execution_id"][:8] for e in recent]

    pipeline_timeline = {
        "type": "line",
        "labels": labels,
        "datasets": [{"label": "Execution Time (s)", "data": [e.get("execution_time_seconds") or 0 for e in recent]}],
    }
    execution_time = {
        "type": "bar",
        "labels": labels,
        "datasets": [{"label": "Execution Time (s)", "data": [e.get("execution_time_seconds") or 0 for e in recent]}],
    }

    cache_stats = metrics.get("cache", {})
    cache_hit_rate = {
        "type": "doughnut",
        "labels": ["Hits", "Misses"],
        "datasets": [{"data": [cache_stats.get("hits", 0), cache_stats.get("misses", 0)]}],
    }

    research_confidence = {
        "type": "line",
        "labels": labels,
        "datasets": [{"label": "Confidence Score", "data": [e.get("confidence_score") or 0 for e in recent]}],
    }
    competitor_count = {
        "type": "bar",
        "labels": labels,
        "datasets": [{"label": "Competitors Discovered", "data": [e.get("competitor_count") or 0 for e in recent]}],
    }

    scheduler_stats = metrics.get("scheduler", {})
    by_status = scheduler_stats.get("by_status", {})
    job_queue = {
        "type": "bar",
        "labels": list(by_status.keys()) or ["no jobs"],
        "datasets": [{"label": "Jobs", "data": list(by_status.values()) or [0]}],
    }

    health_score = {
        "type": "radar",
        "labels": [d.replace("_health", "").title() for d in _HEALTH_DIMENSIONS],
        "datasets": [{"label": "Health", "data": [health_to_score(health.get(d, "unknown")) for d in _HEALTH_DIMENSIONS]}],
    }

    overall = health.get("overall_score", 0.0)
    overall_score = {
        "type": "doughnut",
        "labels": ["Score", "Remaining"],
        "datasets": [{"data": [round(overall * 100, 2), round((1 - overall) * 100, 2)]}],
    }

    return {
        "pipeline_timeline": pipeline_timeline,
        "execution_time": execution_time,
        "cache_hit_rate": cache_hit_rate,
        "research_confidence": research_confidence,
        "competitor_count": competitor_count,
        "job_queue": job_queue,
        "health_score": health_score,
        "overall_score": overall_score,
    }
