import threading
from typing import Any


class SharedStateStore:
    """Thread-safe, in-process key-value store for state shared across a single
    AFOS pipeline run and, for cached workflow outputs, across runs within this
    process's lifetime. This is the storage primitive ExecutionCacheManager is
    built on top of - a raw get/set/has/delete/keys interface with no cache
    policy (TTL, hit/miss tracking, invalidation semantics, execution history)
    of its own. Every method acquires the same re-entrant lock, so concurrent
    reads/writes from multiple threads can never interleave into a corrupted
    intermediate state.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def delete(self, key: str) -> bool:
        with self._lock:
            if key in self._data:
                del self._data[key]
                return True
            return False

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._data.keys())

    def values(self) -> list[Any]:
        with self._lock:
            return list(self._data.values())

    def items(self) -> list[tuple[str, Any]]:
        with self._lock:
            return list(self._data.items())

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._data)
