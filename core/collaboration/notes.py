import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from core.collaboration.activity import get_default_activity_store


class SharedNoteStore:
    """Thread-safe shared notes (free-form venture documentation any team
    member can write to), matching the same store pattern as every other
    collaboration primitive here.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._notes: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []

    def create_note(self, venture_id: str, title: str, body: str, author_id: str, note_id: str | None = None) -> str:
        with self._lock:
            nid = note_id or str(uuid.uuid4())
            entry = {
                "note_id": nid, "venture_id": venture_id, "title": title, "body": body, "author_id": author_id,
                "created_at": time.time(), "updated_at": time.time(),
            }
            self._notes[nid] = entry
            self._order.append(nid)
        get_default_activity_store().record_activity(
            venture_id=venture_id, actor_id=author_id, action="note_created", target_type="note", target_id=nid,
        )
        return nid

    def get_note(self, note_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._notes.get(note_id)
            return dict(entry) if entry is not None else None

    def update_note(self, note_id: str, body: str, editor_id: str | None = None) -> bool:
        with self._lock:
            entry = self._notes.get(note_id)
            if entry is None:
                return False
            entry["body"] = body
            entry["updated_at"] = time.time()
            venture_id = entry["venture_id"]
        get_default_activity_store().record_activity(
            venture_id=venture_id, actor_id=editor_id or "system", action="note_updated", target_type="note", target_id=note_id,
        )
        return True

    def list_notes(self, venture_id: str | None = None, search: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(self._notes[nid]) for nid in reversed(self._order)]
        if venture_id is not None:
            items = [n for n in items if n["venture_id"] == venture_id]
        if search:
            needle = search.lower()
            items = [n for n in items if needle in n["title"].lower() or needle in n["body"].lower()]
        return items

    def clear(self) -> None:
        with self._lock:
            self._notes.clear()
            self._order.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._notes)

    def save_to_disk(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            path.write_text(json.dumps({"notes": self._notes, "order": self._order}, indent=2), encoding="utf-8")

    def load_from_disk(self, path: str | Path) -> bool:
        path = Path(path)
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        with self._lock:
            self._notes = data.get("notes", {})
            self._order = data.get("order", [])
        return True


_default_store = SharedNoteStore()


def get_default_note_store() -> SharedNoteStore:
    return _default_store


def set_default_note_store(store: SharedNoteStore) -> None:
    global _default_store
    _default_store = store


def reset_default_note_store() -> None:
    global _default_store
    _default_store = SharedNoteStore()
