import hashlib
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ExecutionMetadata:
    """Everything AFOS needs to know about one cached workflow execution,
    independent of the (potentially large) output payload itself.
    """

    execution_id: str
    workflow_name: str
    cache_key: str
    created_at: float
    updated_at: float
    execution_time_seconds: float
    ttl_seconds: float | None
    status: str = "completed"

    def is_expired(self, now: float | None = None) -> bool:
        if self.ttl_seconds is None:
            return False
        return (now if now is not None else time.time()) - self.created_at > self.ttl_seconds


@dataclass
class CacheEntry:
    metadata: ExecutionMetadata
    output: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"metadata": asdict(self.metadata), "output": self.output}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CacheEntry":
        return cls(metadata=ExecutionMetadata(**data["metadata"]), output=data["output"])


class ExecutionCacheManager:
    """Production-grade execution cache and duplicate-execution guard for AFOS
    workflow outputs.

    - Thread-safe: every mutating/inspecting operation holds this manager's own
      re-entrant lock (composed with, not delegated to, SharedStateStore's own
      lock - two different locks would allow a read-modify-write race between
      e.g. get() and put() for the same key).
    - TTL support: entries expire based on their own ttl_seconds (falling back
      to this manager's default_ttl_seconds), checked lazily on read.
    - Cache-key based invalidation, plus execution_id based invalidation (the
      identifier is stable across repeated put()s for the same cache_key, so an
      execution_id always names one logical, possibly-refreshed cache slot).
    - Duplicate-execution prevention: try_begin()/end() form a claim/release
      pair a caller uses to ensure only one concurrent computation happens per
      cache_key within a single process.
    - Execution history and hit/miss/size statistics.
    - Resume after interruption: snapshot()/restore() (and the disk-backed
      save_to_disk()/load_from_disk() convenience wrappers) let a fresh process
      recover every *completed* cache entry - anything that was mid-flight when
      the process was interrupted was never put() into the cache, so it is
      correctly absent from a restored snapshot and will simply be recomputed,
      never resumed from a partial/inconsistent state.

    No agent, workflow, or tool logic is duplicated here - this is a pure
    cross-cutting caching layer that wraps an existing invoker's *call*, never
    its implementation.
    """

    def __init__(self, default_ttl_seconds: float | None = 3600.0) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, CacheEntry] = {}
        self._history: list[ExecutionMetadata] = []
        self._in_progress: set[str] = set()
        self._default_ttl_seconds = default_ttl_seconds
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------ #
    # Cache key derivation
    # ------------------------------------------------------------------ #

    @staticmethod
    def make_cache_key(workflow_name: str, **params: Any) -> str:
        """Deterministic, order-independent key derived from the workflow name
        and its call parameters - the same (workflow_name, params) always
        produces the same key, regardless of kwarg ordering.
        """
        payload = json.dumps({"workflow": workflow_name, "params": params}, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ #
    # Core get/put
    # ------------------------------------------------------------------ #

    def get(self, cache_key: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._entries.get(cache_key)
            if entry is None:
                self._misses += 1
                return None
            if entry.metadata.is_expired():
                del self._entries[cache_key]
                self._misses += 1
                return None
            self._hits += 1
            return entry.output

    def put(self, cache_key: str, workflow_name: str, output: dict[str, Any], execution_time_seconds: float, ttl_seconds: float | None = None) -> str:
        with self._lock:
            now = time.time()
            existing = self._entries.get(cache_key)
            execution_id = existing.metadata.execution_id if existing else str(uuid.uuid4())
            metadata = ExecutionMetadata(
                execution_id=execution_id,
                workflow_name=workflow_name,
                cache_key=cache_key,
                created_at=existing.metadata.created_at if existing else now,
                updated_at=now,
                execution_time_seconds=execution_time_seconds,
                ttl_seconds=ttl_seconds if ttl_seconds is not None else self._default_ttl_seconds,
                status="completed",
            )
            self._entries[cache_key] = CacheEntry(metadata=metadata, output=output)
            self._history.append(metadata)
            return execution_id

    # ------------------------------------------------------------------ #
    # Invalidation
    # ------------------------------------------------------------------ #

    def invalidate_key(self, cache_key: str) -> bool:
        with self._lock:
            if cache_key in self._entries:
                del self._entries[cache_key]
                return True
            return False

    def invalidate(self, execution_id: str) -> bool:
        """Invalidate by execution_id rather than cache_key - the identifier a
        caller more naturally holds onto after put() returns it.
        """
        with self._lock:
            for cache_key, entry in list(self._entries.items()):
                if entry.metadata.execution_id == execution_id:
                    del self._entries[cache_key]
                    return True
            return False

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._history.clear()
            self._in_progress.clear()
            self._hits = 0
            self._misses = 0

    # ------------------------------------------------------------------ #
    # Duplicate-execution prevention
    # ------------------------------------------------------------------ #

    def try_begin(self, cache_key: str) -> bool:
        """Claims the right to compute cache_key. Returns True exactly once per
        outstanding claim - a concurrent caller for the same key gets False
        until end() releases it, guaranteeing at most one in-flight computation
        per key within this process, however many threads race to start it.
        """
        with self._lock:
            if cache_key in self._in_progress:
                return False
            self._in_progress.add(cache_key)
            return True

    def end(self, cache_key: str) -> None:
        with self._lock:
            self._in_progress.discard(cache_key)

    def is_in_progress(self, cache_key: str) -> bool:
        with self._lock:
            return cache_key in self._in_progress

    # ------------------------------------------------------------------ #
    # Metadata, history, stats
    # ------------------------------------------------------------------ #

    def get_metadata(self, cache_key: str) -> ExecutionMetadata | None:
        with self._lock:
            entry = self._entries.get(cache_key)
            return entry.metadata if entry else None

    def history(self, workflow_name: str | None = None) -> list[ExecutionMetadata]:
        with self._lock:
            if workflow_name is None:
                return list(self._history)
            return [m for m in self._history if m.workflow_name == workflow_name]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 4) if total else 0.0,
                "miss_rate": round(self._misses / total, 4) if total else 0.0,
                "total_lookups": total,
                "cache_size": len(self._entries),
                "in_progress_count": len(self._in_progress),
                "history_length": len(self._history),
            }

    # ------------------------------------------------------------------ #
    # Resume after interruption
    # ------------------------------------------------------------------ #

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entries": {key: entry.to_dict() for key, entry in self._entries.items()},
                "history": [asdict(m) for m in self._history],
                "hits": self._hits,
                "misses": self._misses,
                "default_ttl_seconds": self._default_ttl_seconds,
            }

    def restore(self, snapshot: dict[str, Any]) -> None:
        """Restores every entry that was actually completed (put()) before the
        snapshot was taken. Entries whose TTL had already lapsed by the time of
        the snapshot are dropped on restore rather than kept as stale hits.
        """
        with self._lock:
            now = time.time()
            restored_entries: dict[str, CacheEntry] = {}
            for key, entry_dict in snapshot.get("entries", {}).items():
                entry = CacheEntry.from_dict(entry_dict)
                if not entry.metadata.is_expired(now):
                    restored_entries[key] = entry
            self._entries = restored_entries
            self._history = [ExecutionMetadata(**m) for m in snapshot.get("history", [])]
            self._hits = snapshot.get("hits", 0)
            self._misses = snapshot.get("misses", 0)
            self._in_progress.clear()

    def save_to_disk(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.snapshot(), indent=2), encoding="utf-8")

    def load_from_disk(self, path: str | Path) -> bool:
        """Returns True if a snapshot file was found and loaded, False if the
        file doesn't exist yet (a fresh cache, not an error - the first run of
        a process has nothing to resume from).
        """
        path = Path(path)
        if not path.exists():
            return False
        self.restore(json.loads(path.read_text(encoding="utf-8")))
        return True
