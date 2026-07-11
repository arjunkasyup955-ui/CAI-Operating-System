import json
from pathlib import Path
from typing import Any

from agents.founder.human_approval.agent import STAGES, get_default_approval_store
from core.collaboration import (
    get_default_activity_store,
    get_default_comment_store,
    get_default_note_store,
    get_default_task_store,
    get_default_team_store,
)
from core.notifications import get_default_notification_store
from workflows.human_approval import compute_approval_metrics, compute_reviewer_activity

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "dashboard"

_STATIC_FILES = {
    "/": ("approvals.html", "text/html; charset=utf-8"),
    "/approvals.html": ("approvals.html", "text/html; charset=utf-8"),
    "/approvals.css": ("approvals.css", "text/css; charset=utf-8"),
    "/approvals.js": ("approvals.js", "application/javascript; charset=utf-8"),
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


def handle_request(method: str, path: str, query: dict[str, list[str]] | None = None, body: bytes = b"") -> tuple[int, str, bytes]:
    """REST-only (GET reads, same convention Phase 5 Component 4's own
    dashboard router established) companion API for the Human Approval
    Engine and Collaboration Layer - a new, additive router alongside (never
    modifying) backend/dashboard/router.py.
    """
    query = query or {}
    method = method.upper()

    if method != "GET":
        return _error(405, f"method not allowed: {method}")

    if path in _STATIC_FILES:
        rel_name, content_type = _STATIC_FILES[path]
        return _serve_static(rel_name, content_type)

    if not path.startswith("/api/"):
        return _error(404, f"not found: {path}")

    venture_id = _q1(query, "venture_id")
    page = _q_int(query, "page", 1)
    page_size = _q_int(query, "page_size", 20)
    store = get_default_approval_store()

    if path == "/api/approvals":
        items = store.list_requests(venture_id=venture_id, stage=_q1(query, "stage"), status=_q1(query, "status"), search=_q1(query, "search"))
        return _json(_paginate(items, page, page_size))

    if path == "/api/approvals/pending":
        items = store.list_pending(venture_id=venture_id, stage=_q1(query, "stage"))
        return _json(_paginate(items, page, page_size))

    if path == "/api/approvals/recent_decisions":
        from agents.founder.human_approval.agent import TERMINAL_STATUSES

        items = [r for r in store.list_requests(venture_id=venture_id) if r["status"] in TERMINAL_STATUSES]
        items.sort(key=lambda r: r["resolved_at"] or 0, reverse=True)
        return _json(_paginate(items, page, page_size))

    if path == "/api/approvals/reviewer_activity":
        items = compute_reviewer_activity(venture_id=venture_id, reviewer_id=_q1(query, "reviewer_id"))
        return _json(_paginate(items, page, page_size))

    if path == "/api/approvals/timeline":
        items = get_default_activity_store().list_activity(venture_id=venture_id, target_type="approval")
        return _json(_paginate(items, page, page_size))

    if path == "/api/approvals/metrics":
        return _json(compute_approval_metrics(venture_id=venture_id))

    if path == "/api/approvals/stages":
        return _json({"stages": list(STAGES)})

    if path.startswith("/api/approvals/") and path.endswith("/history"):
        approval_id = path[len("/api/approvals/"):-len("/history")]
        request = store.get_request(approval_id)
        if request is None:
            return _error(404, f"approval not found: {approval_id}")
        return _json(request["decision_history"])

    if path.startswith("/api/approvals/"):
        approval_id = path[len("/api/approvals/"):]
        request = store.get_request(approval_id)
        if request is None:
            return _error(404, f"approval not found: {approval_id}")
        return _json(request)

    if path == "/api/collaboration/feed":
        items = get_default_activity_store().list_activity(venture_id=venture_id, target_type=_q1(query, "target_type"), search=_q1(query, "search"))
        return _json(_paginate(items, page, page_size))

    if path == "/api/collaboration/comments":
        items = get_default_comment_store().list_comments(
            target_type=_q1(query, "target_type"), target_id=_q1(query, "target_id"), venture_id=venture_id, search=_q1(query, "search"),
        )
        return _json(_paginate(items, page, page_size))

    if path == "/api/collaboration/tasks":
        items = get_default_task_store().list_tasks(venture_id=venture_id, assignee_id=_q1(query, "assignee_id"), status=_q1(query, "status"), search=_q1(query, "search"))
        return _json(_paginate(items, page, page_size))

    if path == "/api/collaboration/team":
        return _json({"members": get_default_team_store().list_members(role=_q1(query, "role"))})

    if path == "/api/collaboration/notes":
        return _json({"notes": get_default_note_store().list_notes(venture_id=venture_id, search=_q1(query, "search"))})

    if path == "/api/notifications":
        recipient_id = _q1(query, "recipient_id")
        unread_only = _q1(query, "unread_only") == "true"
        items = get_default_notification_store().list_notifications(recipient_id=recipient_id, venture_id=venture_id, unread_only=unread_only)
        return _json(_paginate(items, page, page_size))

    return _error(404, f"not found: {path}")
