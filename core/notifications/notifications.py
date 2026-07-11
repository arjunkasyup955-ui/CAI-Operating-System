import threading
import time
import uuid
from typing import Any


class NotificationType:
    APPROVAL_REQUESTED = "approval_requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    TASK_ASSIGNED = "task_assigned"
    MENTION = "mention"
    COMMENT_ADDED = "comment_added"


class NotificationStore:
    """Thread-safe, in-process notification inbox. Deliberately in-process only
    (no email/Slack/webhook delivery) - the same "start in-process, no new
    infra" convention the original AFOS platform design established for
    every kernel service, upgraded only once a real multi-channel delivery
    need exists.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._notifications: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []

    def create_notification(
        self,
        recipient_id: str,
        type: str,
        title: str,
        message: str = "",
        venture_id: str | None = None,
        related_type: str | None = None,
        related_id: str | None = None,
        notification_id: str | None = None,
    ) -> str:
        with self._lock:
            nid = notification_id or str(uuid.uuid4())
            entry = {
                "notification_id": nid,
                "recipient_id": recipient_id,
                "venture_id": venture_id,
                "type": type,
                "title": title,
                "message": message,
                "related_type": related_type,
                "related_id": related_id,
                "read": False,
                "created_at": time.time(),
            }
            self._notifications[nid] = entry
            self._order.append(nid)
            return nid

    def get_notification(self, notification_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._notifications.get(notification_id)
            return dict(entry) if entry is not None else None

    def list_notifications(
        self,
        recipient_id: str | None = None,
        venture_id: str | None = None,
        type: str | None = None,
        unread_only: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(self._notifications[nid]) for nid in reversed(self._order)]
        if recipient_id is not None:
            items = [n for n in items if n["recipient_id"] == recipient_id]
        if venture_id is not None:
            items = [n for n in items if n["venture_id"] == venture_id]
        if type is not None:
            items = [n for n in items if n["type"] == type]
        if unread_only:
            items = [n for n in items if not n["read"]]
        items = items[offset:]
        if limit is not None:
            items = items[:limit]
        return items

    def mark_read(self, notification_id: str) -> bool:
        with self._lock:
            entry = self._notifications.get(notification_id)
            if entry is None:
                return False
            entry["read"] = True
            return True

    def mark_all_read(self, recipient_id: str) -> int:
        with self._lock:
            count = 0
            for entry in self._notifications.values():
                if entry["recipient_id"] == recipient_id and not entry["read"]:
                    entry["read"] = True
                    count += 1
            return count

    def unread_count(self, recipient_id: str) -> int:
        with self._lock:
            return sum(1 for n in self._notifications.values() if n["recipient_id"] == recipient_id and not n["read"])

    def clear(self) -> None:
        with self._lock:
            self._notifications.clear()
            self._order.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._notifications)

    def save_to_disk(self, path) -> None:
        import json
        from pathlib import Path

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            path.write_text(json.dumps({"notifications": self._notifications, "order": self._order}, indent=2), encoding="utf-8")

    def load_from_disk(self, path) -> bool:
        import json
        from pathlib import Path

        path = Path(path)
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        with self._lock:
            self._notifications = data.get("notifications", {})
            self._order = data.get("order", [])
        return True


_default_store = NotificationStore()

NotificationDispatcher = Any  # Callable[[str, str, str, str, ...], str] - documented in dispatch()
_dispatcher = None


def get_default_notification_store() -> NotificationStore:
    return _default_store


def set_default_notification_store(store: NotificationStore) -> None:
    global _default_store
    _default_store = store


def reset_default_notification_store() -> None:
    global _default_store
    _default_store = NotificationStore()


def dispatch(
    recipient_id: str,
    type: str,
    title: str,
    message: str = "",
    venture_id: str | None = None,
    related_type: str | None = None,
    related_id: str | None = None,
) -> str:
    """The single call site every other component (Human Approval, Collaboration)
    uses to send a notification - defaults to writing into the default
    NotificationStore, swappable via set_notification_dispatcher() for tests
    that want to assert on dispatched notifications without a full store, or
    for a future real delivery channel (email/Slack/webhook) to be wired in
    without touching any caller.
    """
    fn = _dispatcher or _default_dispatch
    return fn(recipient_id, type, title, message, venture_id, related_type, related_id)


def _default_dispatch(recipient_id, type, title, message, venture_id, related_type, related_id) -> str:
    return get_default_notification_store().create_notification(
        recipient_id=recipient_id, type=type, title=title, message=message,
        venture_id=venture_id, related_type=related_type, related_id=related_id,
    )


def get_notification_dispatcher():
    return _dispatcher or _default_dispatch


def set_notification_dispatcher(fn) -> None:
    global _dispatcher
    _dispatcher = fn


def reset_notification_dispatcher() -> None:
    global _dispatcher
    _dispatcher = None
