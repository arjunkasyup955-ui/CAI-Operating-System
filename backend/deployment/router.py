import json
from pathlib import Path
from typing import Any

from deployment.history import get_default_deployment_history_store
from deployment.targets import ALL_TARGETS
from workflows.deployment_targets import compute_deployment_health, rollback_deployment

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "dashboard"

_STATIC_FILES = {
    "/": ("deployment.html", "text/html; charset=utf-8"),
    "/deployment.html": ("deployment.html", "text/html; charset=utf-8"),
    "/deployment.css": ("deployment.css", "text/css; charset=utf-8"),
    "/deployment.js": ("deployment.js", "application/javascript; charset=utf-8"),
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
        "items": items[start:end], "page": page, "page_size": page_size, "total": total,
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


def _handle_get(path: str, query: dict[str, list[str]]) -> tuple[int, str, bytes]:
    if path in _STATIC_FILES:
        rel_name, content_type = _STATIC_FILES[path]
        return _serve_static(rel_name, content_type)

    if not path.startswith("/api/deployment"):
        return _error(404, f"not found: {path}")

    venture_id = _q1(query, "venture_id")
    target = _q1(query, "target")
    page = _q_int(query, "page", 1)
    page_size = _q_int(query, "page_size", 20)
    store = get_default_deployment_history_store()

    if path == "/api/deployment/status":
        if target:
            current = store.get_current(venture_id, target) if venture_id else None
            return _json({"target": target, "current": current})
        return _json({t: store.get_current(venture_id, t) if venture_id else None for t in ALL_TARGETS})

    if path == "/api/deployment/environment":
        current_targets = [store.get_current(venture_id, t) for t in ALL_TARGETS] if venture_id else []
        profiles = {r["profile"] for r in current_targets if r is not None}
        return _json({"venture_id": venture_id, "profiles_in_use": sorted(profiles)})

    if path == "/api/deployment/history":
        items = store.list_deployments(venture_id=venture_id, target=target)
        return _json(_paginate(items, page, page_size))

    if path == "/api/deployment/latest":
        items = store.list_deployments(venture_id=venture_id)
        return _json(items[0] if items else None)

    if path == "/api/deployment/health":
        return _json(compute_deployment_health(venture_id=venture_id))

    if path == "/api/deployment/targets":
        return _json({"targets": list(ALL_TARGETS)})

    if path.startswith("/api/deployment/") and path.count("/") == 3:
        deployment_id = path.rsplit("/", 1)[-1]
        record = store.get_deployment(deployment_id)
        if record is None:
            return _error(404, f"deployment not found: {deployment_id}")
        return _json(record)

    return _error(404, f"not found: {path}")


def _handle_post(path: str, body: bytes) -> tuple[int, str, bytes]:
    if path != "/api/deployment/rollback":
        return _error(404, f"not found: {path}")
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return _error(400, "request body must be valid JSON")

    venture_id = payload.get("venture_id")
    target = payload.get("target")
    deployment_id = payload.get("deployment_id")
    if not venture_id or not target or not deployment_id:
        return _error(400, "venture_id, target, and deployment_id are all required")

    result = rollback_deployment(venture_id, target, deployment_id)
    if result is None:
        return _error(404, "deployment not found, or venture_id/target mismatch")
    return _json(result)


def handle_request(method: str, path: str, query: dict[str, list[str]] | None = None, body: bytes = b"") -> tuple[int, str, bytes]:
    """Companion API for the Deployment Generator (deployment/) and the
    zero-modification Dashboard integration it feeds (Phase 5 Component 4,
    never touched). GET reads mirror every prior dashboard router's
    convention; the one deliberate exception is POST /api/deployment/rollback
    - "Rollback Button" is an explicit, real action this component's own
    requirements ask the dashboard to expose, not a read - it only ever
    mutates this component's own new DeploymentHistoryStore (appending a new
    rollback record), never any Phase 0-6 file or real external
    infrastructure.
    """
    query = query or {}
    method = method.upper()
    if method == "GET":
        return _handle_get(path, query)
    if method == "POST":
        return _handle_post(path, body)
    return _error(405, f"method not allowed: {method}")
