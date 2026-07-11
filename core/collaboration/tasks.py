import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from core.collaboration.activity import get_default_activity_store
from core.notifications import NotificationType, dispatch


class TaskStatus:
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TaskStore:
    """Thread-safe collaboration task tracker. Assigning a task dispatches a
    TASK_ASSIGNED notification and records an activity feed entry -
    composition over the already-built notification/activity modules.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []

    def create_task(
        self,
        venture_id: str,
        title: str,
        description: str = "",
        created_by: str = "system",
        assignee_id: str | None = None,
        due_at: float | None = None,
        task_id: str | None = None,
    ) -> str:
        with self._lock:
            tid = task_id or str(uuid.uuid4())
            entry = {
                "task_id": tid, "venture_id": venture_id, "title": title, "description": description,
                "created_by": created_by, "assignee_id": assignee_id, "status": TaskStatus.OPEN,
                "due_at": due_at, "created_at": time.time(), "updated_at": time.time(),
            }
            self._tasks[tid] = entry
            self._order.append(tid)

        get_default_activity_store().record_activity(
            venture_id=venture_id, actor_id=created_by, action="task_created", target_type="task", target_id=tid,
        )
        if assignee_id:
            self._notify_assignment(tid, assignee_id, venture_id, title)
        return tid

    def _notify_assignment(self, task_id: str, assignee_id: str, venture_id: str, title: str) -> None:
        dispatch(
            recipient_id=assignee_id, type=NotificationType.TASK_ASSIGNED, title=f"Task assigned: {title}",
            message=title, venture_id=venture_id, related_type="task", related_id=task_id,
        )
        get_default_activity_store().record_activity(
            venture_id=venture_id, actor_id=assignee_id, action="task_assigned", target_type="task", target_id=task_id,
        )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._tasks.get(task_id)
            return dict(entry) if entry is not None else None

    def list_tasks(
        self,
        venture_id: str | None = None,
        assignee_id: str | None = None,
        status: str | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(self._tasks[tid]) for tid in reversed(self._order)]
        if venture_id is not None:
            items = [t for t in items if t["venture_id"] == venture_id]
        if assignee_id is not None:
            items = [t for t in items if t["assignee_id"] == assignee_id]
        if status is not None:
            items = [t for t in items if t["status"] == status]
        if search:
            needle = search.lower()
            items = [t for t in items if needle in t["title"].lower() or needle in t["description"].lower()]
        items = items[offset:]
        if limit is not None:
            items = items[:limit]
        return items

    def assign_task(self, task_id: str, assignee_id: str) -> bool:
        with self._lock:
            entry = self._tasks.get(task_id)
            if entry is None:
                return False
            entry["assignee_id"] = assignee_id
            entry["updated_at"] = time.time()
            venture_id, title = entry["venture_id"], entry["title"]
        self._notify_assignment(task_id, assignee_id, venture_id, title)
        return True

    def update_task_status(self, task_id: str, status: str) -> bool:
        with self._lock:
            entry = self._tasks.get(task_id)
            if entry is None:
                return False
            entry["status"] = status
            entry["updated_at"] = time.time()
            venture_id = entry["venture_id"]
            actor = entry.get("assignee_id") or entry["created_by"]
        get_default_activity_store().record_activity(
            venture_id=venture_id, actor_id=actor, action=f"task_status_{status}", target_type="task", target_id=task_id,
        )
        return True

    def complete_task(self, task_id: str) -> bool:
        return self.update_task_status(task_id, TaskStatus.COMPLETED)

    def clear(self) -> None:
        with self._lock:
            self._tasks.clear()
            self._order.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._tasks)

    def save_to_disk(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            path.write_text(json.dumps({"tasks": self._tasks, "order": self._order}, indent=2), encoding="utf-8")

    def load_from_disk(self, path: str | Path) -> bool:
        path = Path(path)
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        with self._lock:
            self._tasks = data.get("tasks", {})
            self._order = data.get("order", [])
        return True


_default_store = TaskStore()


def get_default_task_store() -> TaskStore:
    return _default_store


def set_default_task_store(store: TaskStore) -> None:
    global _default_store
    _default_store = store


def reset_default_task_store() -> None:
    global _default_store
    _default_store = TaskStore()
