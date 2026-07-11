import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from core.collaboration.activity import get_default_activity_store
from core.notifications import NotificationType, dispatch

_MENTION_PATTERN = re.compile(r"@([A-Za-z0-9_\-]+)")


def extract_mentions(body: str) -> list[str]:
    """Pure, deterministic @mention extraction - no LLM. Returns the raw
    handles as written (e.g. "@bob" -> "bob"), deduplicated, order-preserving.
    """
    seen: list[str] = []
    for match in _MENTION_PATTERN.finditer(body or ""):
        handle = match.group(1)
        if handle not in seen:
            seen.append(handle)
    return seen


class CommentStore:
    """Thread-safe comment store with basic threading (parent_id) - this is
    what implements "Discussions": a discussion is just a comment thread
    rooted at a top-level comment. Every add_comment() call records an
    activity feed entry and dispatches @mention notifications - composition
    over the already-built ActivityFeedStore/notifications modules, not
    reimplementation.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._comments: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []

    def add_comment(
        self,
        venture_id: str,
        target_type: str,
        target_id: str,
        author_id: str,
        body: str,
        parent_id: str | None = None,
        comment_id: str | None = None,
    ) -> str:
        mentions = extract_mentions(body)
        with self._lock:
            cid = comment_id or str(uuid.uuid4())
            entry = {
                "comment_id": cid, "venture_id": venture_id, "target_type": target_type, "target_id": target_id,
                "author_id": author_id, "body": body, "parent_id": parent_id, "mentions": mentions,
                "created_at": time.time(), "updated_at": time.time(), "deleted": False,
            }
            self._comments[cid] = entry
            self._order.append(cid)

        get_default_activity_store().record_activity(
            venture_id=venture_id, actor_id=author_id, action="comment_added",
            target_type=target_type, target_id=target_id, details={"comment_id": cid},
        )
        for handle in mentions:
            dispatch(
                recipient_id=handle, type=NotificationType.MENTION, title=f"{author_id} mentioned you",
                message=body, venture_id=venture_id, related_type=target_type, related_id=target_id,
            )
        return cid

    def get_comment(self, comment_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._comments.get(comment_id)
            return dict(entry) if entry is not None else None

    def list_comments(
        self,
        target_type: str | None = None,
        target_id: str | None = None,
        venture_id: str | None = None,
        parent_id: str | None = "__any__",
        search: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(self._comments[cid]) for cid in self._order]
        items = [c for c in items if not c["deleted"]]
        if target_type is not None:
            items = [c for c in items if c["target_type"] == target_type]
        if target_id is not None:
            items = [c for c in items if c["target_id"] == target_id]
        if venture_id is not None:
            items = [c for c in items if c["venture_id"] == venture_id]
        if parent_id != "__any__":
            items = [c for c in items if c["parent_id"] == parent_id]
        if search:
            needle = search.lower()
            items = [c for c in items if needle in c["body"].lower()]
        items = items[offset:]
        if limit is not None:
            items = items[:limit]
        return items

    def edit_comment(self, comment_id: str, body: str) -> bool:
        with self._lock:
            entry = self._comments.get(comment_id)
            if entry is None or entry["deleted"]:
                return False
            entry["body"] = body
            entry["mentions"] = extract_mentions(body)
            entry["updated_at"] = time.time()
            return True

    def delete_comment(self, comment_id: str) -> bool:
        with self._lock:
            entry = self._comments.get(comment_id)
            if entry is None:
                return False
            entry["deleted"] = True
            entry["updated_at"] = time.time()
            return True

    def clear(self) -> None:
        with self._lock:
            self._comments.clear()
            self._order.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._comments)

    def save_to_disk(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            path.write_text(json.dumps({"comments": self._comments, "order": self._order}, indent=2), encoding="utf-8")

    def load_from_disk(self, path: str | Path) -> bool:
        path = Path(path)
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        with self._lock:
            self._comments = data.get("comments", {})
            self._order = data.get("order", [])
        return True


_default_store = CommentStore()


def get_default_comment_store() -> CommentStore:
    return _default_store


def set_default_comment_store(store: CommentStore) -> None:
    global _default_store
    _default_store = store


def reset_default_comment_store() -> None:
    global _default_store
    _default_store = CommentStore()
