import logging
import threading
from collections import deque
from typing import Any

CATEGORY_ERROR = "error"
CATEGORY_AGENT = "agent"
CATEGORY_EXECUTION = "execution"
CATEGORY_OTHER = "other"


class LogStore:
    """Thread-safe, bounded ring buffer of structured log entries. Fed by
    LogStoreHandler (a real logging.Handler attached to AFOS's own loggers via
    attach_log_capture()) - every entry here is a genuine emitted log record,
    not a synthetic one.
    """

    def __init__(self, max_entries: int = 2000) -> None:
        self._lock = threading.RLock()
        self._entries: deque[dict[str, Any]] = deque(maxlen=max_entries)

    def append(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._entries.append(entry)

    def list_logs(
        self,
        category: str | None = None,
        level: str | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = list(reversed(self._entries))
        if category is not None:
            items = [e for e in items if e["category"] == category]
        if level is not None:
            items = [e for e in items if e["level"] == level]
        if search:
            needle = search.lower()
            items = [e for e in items if needle in e["message"].lower() or needle in e["logger"].lower()]
        items = items[offset:]
        if limit is not None:
            items = items[:limit]
        return items

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._entries)


def _categorize(record: logging.LogRecord) -> str:
    if record.levelno >= logging.ERROR:
        return CATEGORY_ERROR
    name = record.name or ""
    if name.startswith("afos.agents") or name.startswith("afos.workflows"):
        return CATEGORY_AGENT
    if "execution_cache" in name or "scheduler" in name:
        return CATEGORY_EXECUTION
    return CATEGORY_OTHER


class LogStoreHandler(logging.Handler):
    """Real logging.Handler - registered via logging.getLogger(...).addHandler()
    exactly like any third-party log-shipping handler would be. Never raises
    (a broken log handler must not break the caller it's observing).
    """

    def __init__(self, store: LogStore) -> None:
        super().__init__()
        self._store = store

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "timestamp": record.created,
                "logger": record.name,
                "level": record.levelname,
                "category": _categorize(record),
                "message": record.getMessage(),
            }
            self._store.append(entry)
        except Exception:
            pass


_default_store = LogStore()
_attached_handler: LogStoreHandler | None = None
_attached_logger_name: str | None = None
_attached_previous_level: int = logging.NOTSET
_attach_lock = threading.RLock()


def get_default_log_store() -> LogStore:
    return _default_store


def reset_default_log_store() -> None:
    global _default_store
    _default_store = LogStore()


def attach_log_capture(logger_name: str = "afos", level: int = logging.INFO, store: LogStore | None = None) -> None:
    """Attaches a LogStoreHandler to `logger_name` (default: the shared "afos"
    prefix every AFOS logger is named under, e.g. afos.agents.*,
    afos.workflows.*). Idempotent per call - calling twice attaches a second
    handler, so pair with detach_log_capture() in tests.

    Also raises the target logger's own effective level to `level` if it was
    less permissive (e.g. the process-wide root logger defaults to WARNING,
    per every prior smoke test's own logging.basicConfig call) - a handler's
    level only filters records that already made it past the logger's own
    level check, so without this, attach_log_capture(level=INFO) would
    silently capture nothing whenever the logger's effective level is
    WARNING or higher. The previous level is restored on detach.
    """
    global _attached_handler, _attached_logger_name, _attached_previous_level
    with _attach_lock:
        target_store = store or _default_store
        handler = LogStoreHandler(target_store)
        handler.setLevel(level)
        target_logger = logging.getLogger(logger_name)
        _attached_previous_level = target_logger.level
        if target_logger.getEffectiveLevel() > level:
            target_logger.setLevel(level)
        target_logger.addHandler(handler)
        _attached_handler = handler
        _attached_logger_name = logger_name


def detach_log_capture() -> None:
    global _attached_handler, _attached_logger_name, _attached_previous_level
    with _attach_lock:
        if _attached_handler is not None and _attached_logger_name is not None:
            target_logger = logging.getLogger(_attached_logger_name)
            target_logger.removeHandler(_attached_handler)
            target_logger.setLevel(_attached_previous_level)
        _attached_handler = None
        _attached_logger_name = None
        _attached_previous_level = logging.NOTSET
