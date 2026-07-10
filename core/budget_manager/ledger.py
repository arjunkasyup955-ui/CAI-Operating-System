import sqlite3
from datetime import datetime, timezone
from functools import lru_cache

from core.config import get_settings
from core.event_bus import AFOSEvent, get_event_bus


class BudgetManager:
    """Centralized cost ledger. Never called directly by agents for LLM/tool spend -
    it subscribes to 'llm.invoked' (published by ModelRouter) and 'tool.invoked'
    (published by ToolRegistry) so cost capture is automatic, not opt-in per call site.
    """

    def __init__(self, conn: sqlite3.Connection, monthly_budget_usd: float, venture_budget_usd: float) -> None:
        self._conn = conn
        self._monthly_budget_usd = monthly_budget_usd
        self._venture_budget_usd = venture_budget_usd
        self._init_schema()
        self._subscribe()

    def _init_schema(self) -> None:
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS budget_ledger (
                id INTEGER PRIMARY KEY,
                venture_id TEXT,
                category TEXT NOT NULL,
                description TEXT,
                cost_usd REAL NOT NULL,
                timestamp TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def _subscribe(self) -> None:
        bus = get_event_bus()
        bus.subscribe("llm.invoked", self._on_llm_invoked)
        bus.subscribe("tool.invoked", self._on_tool_invoked)

    def _on_llm_invoked(self, event: AFOSEvent) -> None:
        cost = event.payload.get("cost_usd", 0.0)
        if cost > 0:
            desc = f"{event.payload.get('provider')}/{event.payload.get('model')}"
            self.record(event.venture_id, "llm", desc, cost)

    def _on_tool_invoked(self, event: AFOSEvent) -> None:
        cost = event.payload.get("cost_usd", 0.0)
        if cost > 0:
            self.record(event.venture_id, "tool", event.payload.get("tool", "unknown"), cost)

    def record(self, venture_id: str | None, category: str, description: str, cost_usd: float) -> None:
        self._conn.execute(
            "INSERT INTO budget_ledger (venture_id, category, description, cost_usd, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (venture_id, category, description, cost_usd, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()
        self._check_thresholds(venture_id)

    def venture_spend(self, venture_id: str) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM budget_ledger WHERE venture_id = ?", (venture_id,)
        ).fetchone()
        return row[0]

    def monthly_spend(self) -> float:
        month_start = datetime.now(timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM budget_ledger WHERE timestamp >= ?", (month_start,)
        ).fetchone()
        return row[0]

    def _check_thresholds(self, venture_id: str | None) -> None:
        bus = get_event_bus()
        if venture_id:
            spend = self.venture_spend(venture_id)
            if spend > self._venture_budget_usd:
                bus.publish(
                    AFOSEvent(
                        type="budget.threshold_exceeded",
                        source_agent="budget_manager",
                        venture_id=venture_id,
                        payload={"scope": "venture", "limit_usd": self._venture_budget_usd, "spend_usd": spend},
                    )
                )

        monthly = self.monthly_spend()
        if monthly > self._monthly_budget_usd:
            bus.publish(
                AFOSEvent(
                    type="budget.threshold_exceeded",
                    source_agent="budget_manager",
                    venture_id=venture_id,
                    payload={"scope": "monthly", "limit_usd": self._monthly_budget_usd, "spend_usd": monthly},
                )
            )


@lru_cache
def get_budget_manager() -> BudgetManager:
    from core.memory_gateway import get_memory_gateway

    settings = get_settings()
    gateway = get_memory_gateway()
    return BudgetManager(gateway.knowledge_conn, settings.afos_monthly_budget_usd, settings.afos_venture_budget_usd)
