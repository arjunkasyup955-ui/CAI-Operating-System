import threading
import time
import uuid
from typing import Any


class ExecutionHistoryStore:
    """Thread-safe, structured record of pipeline executions for the Founder
    Dashboard - deliberately independent of core.execution_cache's internal
    schema (that store keys by cache_key/workflow output; this one keys by a
    dashboard-facing execution_id and always carries the fields the dashboard
    needs - venture_id, confidence_score, competitor_count - regardless of
    which pipeline produced them). A caller records one entry per completed
    (or failed/running) execution via record_execution(); nothing here
    recomputes or re-derives a pipeline's own logic.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._executions: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []

    def record_execution(
        self,
        pipeline_name: str,
        venture_id: str,
        status: str,
        execution_time_seconds: float | None = None,
        confidence_score: float | None = None,
        competitor_count: int | None = None,
        metadata: dict[str, Any] | None = None,
        execution_id: str | None = None,
    ) -> str:
        with self._lock:
            eid = execution_id or str(uuid.uuid4())
            entry = {
                "execution_id": eid,
                "pipeline_name": pipeline_name,
                "venture_id": venture_id,
                "status": status,
                "execution_time_seconds": execution_time_seconds,
                "confidence_score": confidence_score,
                "competitor_count": competitor_count,
                "metadata": metadata or {},
                "recorded_at": time.time(),
            }
            if eid not in self._executions:
                self._order.append(eid)
            self._executions[eid] = entry
            return eid

    def get_execution(self, execution_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._executions.get(execution_id)
            return dict(entry) if entry is not None else None

    def list_executions(
        self,
        venture_id: str | None = None,
        pipeline_name: str | None = None,
        status: str | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(self._executions[eid]) for eid in reversed(self._order)]
        if venture_id is not None:
            items = [e for e in items if e["venture_id"] == venture_id]
        if pipeline_name is not None:
            items = [e for e in items if e["pipeline_name"] == pipeline_name]
        if status is not None:
            items = [e for e in items if e["status"] == status]
        if search:
            needle = search.lower()
            items = [e for e in items if needle in e["venture_id"].lower() or needle in e["pipeline_name"].lower() or needle in e["execution_id"].lower()]
        items = items[offset:]
        if limit is not None:
            items = items[:limit]
        return items

    def list_projects(self) -> list[str]:
        with self._lock:
            return sorted({e["venture_id"] for e in self._executions.values()})

    def compare_executions(self, execution_id_a: str, execution_id_b: str) -> dict[str, Any] | None:
        a = self.get_execution(execution_id_a)
        b = self.get_execution(execution_id_b)
        if a is None or b is None:
            return None
        fields = ["status", "execution_time_seconds", "confidence_score", "competitor_count"]
        return {"a": a, "b": b, "diff": {f: {"a": a.get(f), "b": b.get(f)} for f in fields}}

    def clear(self) -> None:
        with self._lock:
            self._executions.clear()
            self._order.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._executions)


_default_store = ExecutionHistoryStore()


def get_default_execution_history() -> ExecutionHistoryStore:
    return _default_store


def set_default_execution_history(store: ExecutionHistoryStore) -> None:
    global _default_store
    _default_store = store


def reset_default_execution_history() -> None:
    global _default_store
    _default_store = ExecutionHistoryStore()
