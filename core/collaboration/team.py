import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from core.collaboration.permissions import Role, role_has_permission


class TeamStore:
    """Thread-safe registry of team members and their roles - the identity
    layer every other collaboration/approval feature (comments, tasks,
    reviewer assignment, permission checks) is built on top of.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._members: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []

    def add_member(self, name: str, email: str = "", role: str = Role.CONTRIBUTOR, member_id: str | None = None) -> str:
        with self._lock:
            mid = member_id or str(uuid.uuid4())
            entry = {
                "member_id": mid, "name": name, "email": email, "role": role,
                "created_at": time.time(), "updated_at": time.time(),
            }
            if mid not in self._members:
                self._order.append(mid)
            self._members[mid] = entry
            return mid

    def get_member(self, member_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._members.get(member_id)
            return dict(entry) if entry is not None else None

    def list_members(self, role: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(self._members[mid]) for mid in self._order]
        if role is not None:
            items = [m for m in items if m["role"] == role]
        return items

    def update_role(self, member_id: str, role: str) -> bool:
        with self._lock:
            entry = self._members.get(member_id)
            if entry is None:
                return False
            entry["role"] = role
            entry["updated_at"] = time.time()
            return True

    def remove_member(self, member_id: str) -> bool:
        with self._lock:
            if member_id not in self._members:
                return False
            del self._members[member_id]
            self._order.remove(member_id)
            return True

    def has_permission(self, member_id: str, permission: str) -> bool:
        member = self.get_member(member_id)
        if member is None:
            return False
        return role_has_permission(member["role"], permission)

    def clear(self) -> None:
        with self._lock:
            self._members.clear()
            self._order.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._members)

    def save_to_disk(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            path.write_text(json.dumps({"members": self._members, "order": self._order}, indent=2), encoding="utf-8")

    def load_from_disk(self, path: str | Path) -> bool:
        path = Path(path)
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        with self._lock:
            self._members = data.get("members", {})
            self._order = data.get("order", [])
        return True


_default_store = TeamStore()


def get_default_team_store() -> TeamStore:
    return _default_store


def set_default_team_store(store: TeamStore) -> None:
    global _default_store
    _default_store = store


def reset_default_team_store() -> None:
    global _default_store
    _default_store = TeamStore()
