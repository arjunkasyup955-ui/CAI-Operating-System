import json
from pathlib import Path
from typing import Any

from backend.dashboard.charts import build_chart_data
from backend.dashboard.export import export
from backend.dashboard.jobs import get_job, submit_job
from core.metrics import collect_metrics, compute_health
from core.metrics.collector import get_observed_scheduler
from core.observability import get_default_execution_history, get_default_log_store, get_pipeline_events

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "dashboard"

_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "application/javascript; charset=utf-8"),
}


def _q1(query: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
    values = query.get(key)
    return values[0] if values else default


def _q_int(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = _q1(query, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _paginate(items: list[Any], page: int, page_size: int) -> dict[str, Any]:
    page = max(page, 1)
    page_size = max(page_size, 1)
    start = (page - 1) * page_size
    end = start + page_size
    total = len(items)
    return {
        "items": items[start:end],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size if page_size else 0,
    }


def _json(data: Any, status: int = 200) -> tuple[int, str, bytes]:
    return status, "application/json; charset=utf-8", json.dumps(data, default=str).encode("utf-8")


def _error(status: int, message: str) -> tuple[int, str, bytes]:
    return _json({"error": message}, status=status)


def _serve_static(rel_name: str, content_type: str) -> tuple[int, str, bytes]:
    path = FRONTEND_DIR / rel_name
    if not path.exists():
        return _error(404, f"static asset not found: {rel_name}")
    return 200, content_type, path.read_bytes()


def _handle_submit(body: bytes) -> tuple[int, str, bytes]:
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error(400, "request body must be valid JSON")
    if not isinstance(payload, dict):
        return _error(400, "request body must be a JSON object")

    idea = payload.get("idea")
    if not isinstance(idea, str) or not idea.strip():
        return _error(400, "'idea' is required and must be a non-empty string")

    venture_id = payload.get("venture_id")
    if venture_id is not None and not isinstance(venture_id, str):
        return _error(400, "'venture_id' must be a string if provided")

    research_depth = payload.get("research_depth")
    if research_depth is not None and not isinstance(research_depth, str):
        return _error(400, "'research_depth' must be a string if provided")

    job_id = submit_job(idea, venture_id, research_depth or "standard")
    return _json({"job_id": job_id, "status": "queued"}, status=202)


def _current_metrics_and_health(venture_id: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    metrics = collect_metrics(venture_id=venture_id)
    health = compute_health(metrics)
    return metrics, health


def handle_request(method: str, path: str, query: dict[str, list[str]] | None = None, body: bytes = b"") -> tuple[int, str, bytes]:
    """Pure request router: (method, path, query, body) -> (status, content_type,
    body_bytes). Mostly a read/export surface over data other components
    already produce (GET-only, 405 otherwise), with one deliberate exception:
    POST /api/dashboard/submissions starts a founder_dashboard pipeline run
    in a background job (backend/dashboard/jobs.py) and returns a job_id -
    the only server-side mutation this router performs. Deliberately
    decoupled from any actual socket/HTTP server machinery, so both the real
    stdlib server (backend/dashboard/server.py) and a smoke test can call
    this directly.
    """
    query = query or {}
    method = method.upper()

    if method == "POST":
        if path == "/api/dashboard/submissions":
            return _handle_submit(body)
        return _error(405, f"method not allowed: {method}")

    if method != "GET":
        return _error(405, f"method not allowed: {method}")

    if path in _STATIC_FILES:
        rel_name, content_type = _STATIC_FILES[path]
        return _serve_static(rel_name, content_type)

    if not path.startswith("/api/dashboard"):
        return _error(404, f"not found: {path}")

    venture_id = _q1(query, "venture_id")
    page = _q_int(query, "page", 1)
    page_size = _q_int(query, "page_size", 20)

    if path == "/api/dashboard/metrics":
        metrics, _ = _current_metrics_and_health(venture_id)
        return _json(metrics)

    if path == "/api/dashboard/health":
        metrics, health = _current_metrics_and_health(venture_id)
        return _json(health)

    if path == "/api/dashboard/projects":
        return _json({"projects": get_default_execution_history().list_projects()})

    if path == "/api/dashboard/history":
        store = get_default_execution_history()
        items = store.list_executions(
            venture_id=venture_id,
            pipeline_name=_q1(query, "pipeline_name"),
            status=_q1(query, "status"),
            search=_q1(query, "search"),
        )
        return _json(_paginate(items, page, page_size))

    if path.startswith("/api/dashboard/history/"):
        execution_id = path[len("/api/dashboard/history/"):]
        entry = get_default_execution_history().get_execution(execution_id)
        if entry is None:
            return _error(404, f"execution not found: {execution_id}")
        return _json(entry)

    if path.startswith("/api/dashboard/submissions/"):
        job_id = path[len("/api/dashboard/submissions/"):]
        job = get_job(job_id)
        if job is None:
            return _error(404, f"job not found: {job_id}")
        return _json(job)

    if path == "/api/dashboard/compare":
        id_a, id_b = _q1(query, "a"), _q1(query, "b")
        if not id_a or not id_b:
            return _error(400, "both 'a' and 'b' execution_id query params are required")
        result = get_default_execution_history().compare_executions(id_a, id_b)
        if result is None:
            return _error(404, "one or both execution_ids not found")
        return _json(result)

    if path == "/api/dashboard/jobs":
        scheduler = get_observed_scheduler()
        if scheduler is None:
            return _json({"registered": False, **_paginate([], page, page_size)})
        jobs = [j.to_dict() for j in scheduler.list_jobs(status=_q1(query, "status"), job_type=_q1(query, "job_type"))]
        search = _q1(query, "search")
        if search:
            needle = search.lower()
            jobs = [j for j in jobs if needle in j["job_id"].lower() or needle in j["job_type"].lower()]
        result = _paginate(jobs, page, page_size)
        result["registered"] = True
        result["stats"] = scheduler.stats()
        return _json(result)

    if path == "/api/dashboard/logs":
        items = get_default_log_store().list_logs(
            category=_q1(query, "category"),
            level=_q1(query, "level"),
            search=_q1(query, "search"),
        )
        return _json(_paginate(items, page, page_size))

    if path == "/api/dashboard/events":
        items = get_pipeline_events(
            venture_id=venture_id,
            event_type=_q1(query, "event_type"),
            search=_q1(query, "search"),
        )
        return _json(_paginate(items, page, page_size))

    if path == "/api/dashboard/charts":
        metrics, health = _current_metrics_and_health(venture_id)
        history = get_default_execution_history().list_executions(venture_id=venture_id)
        return _json(build_chart_data(metrics, history, health))

    if path.startswith("/api/dashboard/export/"):
        fmt = path[len("/api/dashboard/export/"):]
        metrics, health = _current_metrics_and_health(venture_id)
        history = get_default_execution_history().list_executions(venture_id=venture_id)
        try:
            body_bytes, content_type = export(fmt, metrics, health, history)
        except ValueError as exc:
            return _error(400, str(exc))
        return 200, content_type, body_bytes

    return _error(404, f"not found: {path}")
