import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class WorkspaceHistoryStore:
    """Thread-safe audit trail of WORKSPACE-level actions (project created/
    opened/archived/deleted/cloned) - deliberately distinct from a project's
    own pipeline "Execution History" memory kind (workspace/memory/), which
    is a pure read-through over Phase 5 Component 4's
    core.observability.ExecutionHistoryStore. This one is new, first-party
    state: nothing elsewhere in AFOS already tracks "when was this project
    opened/archived/cloned".
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []

    def record_event(self, project_id: str, action: str, details: dict[str, Any] | None = None, event_id: str | None = None) -> str:
        with self._lock:
            eid = event_id or str(uuid.uuid4())
            entry = {"event_id": eid, "project_id": project_id, "action": action, "details": details or {}, "timestamp": time.time()}
            self._events[eid] = entry
            self._order.append(eid)
            return eid

    def list_events(
        self,
        project_id: str | None = None,
        action: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(self._events[eid]) for eid in reversed(self._order)]
        if project_id is not None:
            items = [e for e in items if e["project_id"] == project_id]
        if action is not None:
            items = [e for e in items if e["action"] == action]
        items = items[offset:]
        if limit is not None:
            items = items[:limit]
        return items

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._order.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._events)

    def save_to_disk(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            path.write_text(json.dumps({"events": self._events, "order": self._order}, indent=2), encoding="utf-8")

    def load_from_disk(self, path: str | Path) -> bool:
        path = Path(path)
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        with self._lock:
            self._events = data.get("events", {})
            self._order = data.get("order", [])
        return True


_default_store = WorkspaceHistoryStore()


def get_default_workspace_history_store() -> WorkspaceHistoryStore:
    return _default_store


def set_default_workspace_history_store(store: WorkspaceHistoryStore) -> None:
    global _default_store
    _default_store = store


def reset_default_workspace_history_store() -> None:
    global _default_store
    _default_store = WorkspaceHistoryStore()
