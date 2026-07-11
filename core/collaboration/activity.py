import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class ActivityFeedStore:
    """Thread-safe, append-only activity feed - the single timeline every
    other collaboration/approval feature writes one entry into whenever
    something noteworthy happens (member added, comment posted, task
    assigned/completed, approval decision, mention). Composition, not
    duplication: this store never re-derives an event, it's just where every
    other store's own action gets logged for a unified feed.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []

    def record_activity(
        self,
        venture_id: str,
        actor_id: str,
        action: str,
        target_type: str,
        target_id: str,
        details: dict[str, Any] | None = None,
        entry_id: str | None = None,
    ) -> str:
        with self._lock:
            eid = entry_id or str(uuid.uuid4())
            entry = {
                "entry_id": eid, "venture_id": venture_id, "actor_id": actor_id, "action": action,
                "target_type": target_type, "target_id": target_id, "details": details or {},
                "timestamp": time.time(),
            }
            self._entries[eid] = entry
            self._order.append(eid)
            return eid

    def list_activity(
        self,
        venture_id: str | None = None,
        actor_id: str | None = None,
        target_type: str | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(self._entries[eid]) for eid in reversed(self._order)]
        if venture_id is not None:
            items = [e for e in items if e["venture_id"] == venture_id]
        if actor_id is not None:
            items = [e for e in items if e["actor_id"] == actor_id]
        if target_type is not None:
            items = [e for e in items if e["target_type"] == target_type]
        if search:
            needle = search.lower()
            items = [e for e in items if needle in e["action"].lower() or needle in e["actor_id"].lower()]
        items = items[offset:]
        if limit is not None:
            items = items[:limit]
        return items

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._order.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def save_to_disk(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            path.write_text(json.dumps({"entries": self._entries, "order": self._order}, indent=2), encoding="utf-8")

    def load_from_disk(self, path: str | Path) -> bool:
        path = Path(path)
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        with self._lock:
            self._entries = data.get("entries", {})
            self._order = data.get("order", [])
        return True


_default_store = ActivityFeedStore()


def get_default_activity_store() -> ActivityFeedStore:
    return _default_store


def set_default_activity_store(store: ActivityFeedStore) -> None:
    global _default_store
    _default_store = store


def reset_default_activity_store() -> None:
    global _default_store
    _default_store = ActivityFeedStore()
