import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from workspace.history.store import WorkspaceHistoryStore, get_default_workspace_history_store
from workspace.memory.store import ALL_KINDS, ProjectMemoryStore, get_default_project_memory_store


class ProjectStatus:
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class ProjectStore:
    """Thread-safe multi-project workspace registry. A project's own
    project_id IS its venture_id (the same identifier every other AFOS store
    - ExecutionHistoryStore, ApprovalStore, DeploymentHistoryStore, this
    component's own ProjectMemoryStore - already indexes by), so opening a
    project needs no translation layer to reach its memory.

    Takes its WorkspaceHistoryStore and ProjectMemoryStore as constructor
    dependencies (dependency injection - defaults to the process-wide
    singletons, but a caller can inject isolated instances for tests).
    Delete is a soft delete (status=DELETED, memory untouched) - consistent
    with every other soft-delete in AFOS (e.g. core.collaboration's
    CommentStore) and with "No regressions": a deleted project's history/
    memory is never destroyed, only hidden from default listings.
    """

    def __init__(self, history_store: WorkspaceHistoryStore | None = None, memory_store: ProjectMemoryStore | None = None) -> None:
        self._lock = threading.RLock()
        self._projects: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._history_store = history_store or get_default_workspace_history_store()
        self._memory_store = memory_store or get_default_project_memory_store()

    def create_project(self, name: str, description: str = "", tags: list[str] | None = None, project_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            pid = project_id or str(uuid.uuid4())
            now = time.time()
            entry = {
                "project_id": pid, "venture_id": pid, "name": name, "description": description,
                "tags": list(tags or []), "status": ProjectStatus.ACTIVE,
                "created_at": now, "updated_at": now, "last_opened_at": None, "cloned_from": None,
            }
            self._projects[pid] = entry
            self._order.append(pid)
        self._history_store.record_event(pid, "project_created", {"name": name})
        return self.get_project(pid)

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._projects.get(project_id)
            return json.loads(json.dumps(entry)) if entry is not None else None

    def list_projects(self, status: str | None = None, tag: str | None = None, search: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = [json.loads(json.dumps(self._projects[pid])) for pid in self._order]
        if status is not None:
            items = [p for p in items if p["status"] == status]
        else:
            items = [p for p in items if p["status"] != ProjectStatus.DELETED]
        if tag is not None:
            items = [p for p in items if tag in p["tags"]]
        if search:
            needle = search.lower()
            items = [p for p in items if needle in p["name"].lower() or needle in p["description"].lower()]
        return items

    def open_project(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._projects.get(project_id)
            if entry is None or entry["status"] == ProjectStatus.DELETED:
                return None
            entry["last_opened_at"] = time.time()
        self._history_store.record_event(project_id, "project_opened")
        return self.get_project(project_id)

    def recent_projects(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            items = [json.loads(json.dumps(p)) for p in self._projects.values() if p["status"] != ProjectStatus.DELETED and p["last_opened_at"] is not None]
        items.sort(key=lambda p: p["last_opened_at"], reverse=True)
        return items[:limit]

    def archive_project(self, project_id: str) -> bool:
        return self._set_status(project_id, ProjectStatus.ARCHIVED, "project_archived", from_statuses=(ProjectStatus.ACTIVE,))

    def unarchive_project(self, project_id: str) -> bool:
        return self._set_status(project_id, ProjectStatus.ACTIVE, "project_unarchived", from_statuses=(ProjectStatus.ARCHIVED,))

    def delete_project(self, project_id: str) -> bool:
        return self._set_status(project_id, ProjectStatus.DELETED, "project_deleted", from_statuses=(ProjectStatus.ACTIVE, ProjectStatus.ARCHIVED))

    def _set_status(self, project_id: str, new_status: str, event: str, from_statuses: tuple[str, ...]) -> bool:
        with self._lock:
            entry = self._projects.get(project_id)
            if entry is None or entry["status"] not in from_statuses:
                return False
            entry["status"] = new_status
            entry["updated_at"] = time.time()
        self._history_store.record_event(project_id, event)
        return True

    def clone_project(self, project_id: str, new_name: str | None = None, include_memory: bool = True) -> dict[str, Any] | None:
        """Creates a new project (a fresh project_id/venture_id) copying the
        source's metadata, and - by default - deep-copying its research/
        competitor/decision/mvp/builder memory entries so the clone starts
        with the same pipeline history. Deliberately does NOT copy Deployment
        Memory, Approval History, or Execution History: those represent real,
        already-happened events (a real deployment, a real human approval
        decision, a real cache/scheduler execution) tied to the ORIGINAL
        venture - copying them onto a clone would misleadingly imply the
        clone itself was deployed/approved/executed, which it wasn't.
        """
        source = self.get_project(project_id)
        if source is None:
            return None
        clone = self.create_project(
            name=new_name or f"{source['name']} (clone)", description=source["description"], tags=list(source["tags"]),
        )
        with self._lock:
            entry = self._projects[clone["project_id"]]
            entry["cloned_from"] = project_id

        if include_memory:
            for kind in ALL_KINDS:
                for original_entry in reversed(self._memory_store.list_entries(kind, project_id)):
                    self._memory_store.record(kind, clone["project_id"], original_entry["data"], source=f"cloned_from:{project_id}")

        self._history_store.record_event(project_id, "project_cloned_from", {"clone_id": clone["project_id"]})
        self._history_store.record_event(clone["project_id"], "project_cloned_to", {"source_id": project_id})
        return self.get_project(clone["project_id"])

    def update_metadata(self, project_id: str, name: str | None = None, description: str | None = None) -> bool:
        with self._lock:
            entry = self._projects.get(project_id)
            if entry is None:
                return False
            if name is not None:
                entry["name"] = name
            if description is not None:
                entry["description"] = description
            entry["updated_at"] = time.time()
        self._history_store.record_event(project_id, "project_metadata_updated")
        return True

    def add_tag(self, project_id: str, tag: str) -> bool:
        with self._lock:
            entry = self._projects.get(project_id)
            if entry is None:
                return False
            if tag not in entry["tags"]:
                entry["tags"].append(tag)
                entry["updated_at"] = time.time()
        return True

    def remove_tag(self, project_id: str, tag: str) -> bool:
        with self._lock:
            entry = self._projects.get(project_id)
            if entry is None or tag not in entry["tags"]:
                return False
            entry["tags"].remove(tag)
            entry["updated_at"] = time.time()
        return True

    def clear(self) -> None:
        with self._lock:
            self._projects.clear()
            self._order.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._projects)

    def save_to_disk(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            path.write_text(json.dumps({"projects": self._projects, "order": self._order}, indent=2), encoding="utf-8")

    def load_from_disk(self, path: str | Path) -> bool:
        path = Path(path)
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        with self._lock:
            self._projects = data.get("projects", {})
            self._order = data.get("order", [])
        return True


_default_store = ProjectStore()


def get_default_project_store() -> ProjectStore:
    return _default_store


def set_default_project_store(store: ProjectStore) -> None:
    global _default_store
    _default_store = store


def reset_default_project_store() -> None:
    global _default_store
    _default_store = ProjectStore()
