import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class MemoryKind:
    """The 5 pipeline stages with no existing persistent store elsewhere in
    AFOS (Deployment/Approval/Execution memory are read-through wrappers over
    Phase 5 Components 7/6/4's own stores instead - see
    workspace/memory/aggregator.py - so they are never duplicated here).
    """

    RESEARCH = "research"
    COMPETITOR = "competitor"
    DECISION = "decision"
    MVP = "mvp"
    BUILDER = "builder"


ALL_KINDS = (MemoryKind.RESEARCH, MemoryKind.COMPETITOR, MemoryKind.DECISION, MemoryKind.MVP, MemoryKind.BUILDER)


class ProjectMemoryStore:
    """Thread-safe, kind-and-venture-partitioned memory store. Generic on
    purpose - one implementation serving all 5 kinds rather than 5 near-
    identical classes ("no duplicated code"). A caller records a stage's
    output explicitly (e.g. after calling agents.founder.mvp_planner.agent.
    run_mvp_planner(...), it calls record(MemoryKind.MVP, venture_id,
    result)) - this module never calls into any Phase 0-7 pipeline itself,
    it only stores what a caller hands it, keeping "Do NOT modify Components
    1-7" trivially true (nothing here even imports them).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, list[dict[str, Any]]] = {}

    @staticmethod
    def _bucket_key(kind: str, venture_id: str) -> str:
        return f"{kind}:{venture_id}"

    def record(self, kind: str, venture_id: str, data: dict[str, Any], source: str = "", entry_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            entry = {
                "entry_id": entry_id or str(uuid.uuid4()), "kind": kind, "venture_id": venture_id,
                "data": data, "source": source, "recorded_at": time.time(),
            }
            bucket = self._entries.setdefault(self._bucket_key(kind, venture_id), [])
            bucket.append(entry)
            return dict(entry)

    def get_latest(self, kind: str, venture_id: str) -> dict[str, Any] | None:
        with self._lock:
            bucket = self._entries.get(self._bucket_key(kind, venture_id))
            return dict(bucket[-1]) if bucket else None

    def list_entries(self, kind: str, venture_id: str) -> list[dict[str, Any]]:
        with self._lock:
            bucket = self._entries.get(self._bucket_key(kind, venture_id), [])
            return [dict(e) for e in reversed(bucket)]

    def clear_project(self, venture_id: str) -> None:
        with self._lock:
            for kind in ALL_KINDS:
                self._entries.pop(self._bucket_key(kind, venture_id), None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def size(self) -> int:
        with self._lock:
            return sum(len(bucket) for bucket in self._entries.values())

    def save_to_disk(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            path.write_text(json.dumps(self._entries, indent=2), encoding="utf-8")

    def load_from_disk(self, path: str | Path) -> bool:
        path = Path(path)
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        with self._lock:
            self._entries = data
        return True


_default_store = ProjectMemoryStore()


def get_default_project_memory_store() -> ProjectMemoryStore:
    return _default_store


def set_default_project_memory_store(store: ProjectMemoryStore) -> None:
    global _default_store
    _default_store = store


def reset_default_project_memory_store() -> None:
    global _default_store
    _default_store = ProjectMemoryStore()
