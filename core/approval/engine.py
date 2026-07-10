import sqlite3
from datetime import datetime, timezone
from enum import StrEnum
from functools import lru_cache
from typing import Any, Literal

from langgraph.types import interrupt
from pydantic import BaseModel, Field

from core.event_bus import AFOSEvent, get_event_bus


class ApprovalPolicy(StrEnum):
    ALWAYS_APPROVE = "always_approve"
    AUTO_APPROVE_UNDER_LIMIT = "auto_approve_under_limit"
    RISK_BASED = "risk_based"
    EMERGENCY_STOP = "emergency_stop"


RiskLevel = Literal["low", "medium", "high", "critical"]
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_DEFAULT_POLICY_CONFIG = {
    "policy": ApprovalPolicy.RISK_BASED,
    "threshold_usd": 0.0,
    "min_risk_for_approval": "high",
}


class ApprovalRequest(BaseModel):
    action: str
    venture_id: str
    risk_level: RiskLevel = "low"
    amount_usd: float = 0.0
    details: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecision(BaseModel):
    approved: bool
    reason: str
    auto: bool  # True if decided without pausing for a human


class ApprovalPolicyEngine:
    """Configurable per-venture, per-action-type approval policy. Deploy/spend/CRM-write
    gates all call this; it decides *whether* to pause via LangGraph's interrupt() - the
    graph doesn't hardcode "always pause here" anymore.

    Policies and emergency-stop state are persisted to SQLite so a process restart
    doesn't silently reset a venture back to the default policy or clear an active
    emergency stop - both are loaded back from disk on init.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._policies: dict[str, dict[str, dict]] = {}
        self._emergency_stopped: set[str] = set()
        self._init_schema()
        self._load_state()

    def _init_schema(self) -> None:
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS approval_policies (
                venture_id TEXT NOT NULL,
                action TEXT NOT NULL,
                policy TEXT NOT NULL,
                threshold_usd REAL NOT NULL DEFAULT 0,
                min_risk_for_approval TEXT NOT NULL DEFAULT 'high',
                PRIMARY KEY (venture_id, action)
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS emergency_stops (
                venture_id TEXT PRIMARY KEY,
                stopped_at TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def _load_state(self) -> None:
        for venture_id, action, policy, threshold_usd, min_risk in self._conn.execute(
            "SELECT venture_id, action, policy, threshold_usd, min_risk_for_approval FROM approval_policies"
        ):
            self._policies.setdefault(venture_id, {})[action] = {
                "policy": ApprovalPolicy(policy),
                "threshold_usd": threshold_usd,
                "min_risk_for_approval": min_risk,
            }
        for (venture_id,) in self._conn.execute("SELECT venture_id FROM emergency_stops"):
            self._emergency_stopped.add(venture_id)

    def set_policy(
        self,
        venture_id: str,
        action: str,
        policy: ApprovalPolicy,
        threshold_usd: float = 0.0,
        min_risk_for_approval: RiskLevel = "high",
    ) -> None:
        self._policies.setdefault(venture_id, {})[action] = {
            "policy": policy,
            "threshold_usd": threshold_usd,
            "min_risk_for_approval": min_risk_for_approval,
        }
        self._conn.execute(
            """INSERT INTO approval_policies (venture_id, action, policy, threshold_usd, min_risk_for_approval)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(venture_id, action) DO UPDATE SET
                 policy = excluded.policy,
                 threshold_usd = excluded.threshold_usd,
                 min_risk_for_approval = excluded.min_risk_for_approval""",
            (venture_id, action, policy.value, threshold_usd, min_risk_for_approval),
        )
        self._conn.commit()

    def get_policy(self, venture_id: str, action: str) -> dict[str, Any] | None:
        return self._policies.get(venture_id, {}).get(action)

    def emergency_stop(self, venture_id: str) -> None:
        self._emergency_stopped.add(venture_id)
        self._conn.execute(
            "INSERT OR REPLACE INTO emergency_stops (venture_id, stopped_at) VALUES (?, ?)",
            (venture_id, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()
        get_event_bus().publish(
            AFOSEvent(type="approval.emergency_stop", source_agent="approval_engine", venture_id=venture_id)
        )

    def clear_emergency_stop(self, venture_id: str) -> None:
        self._emergency_stopped.discard(venture_id)
        self._conn.execute("DELETE FROM emergency_stops WHERE venture_id = ?", (venture_id,))
        self._conn.commit()

    def is_emergency_stopped(self, venture_id: str) -> bool:
        return venture_id in self._emergency_stopped

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        if req.venture_id in self._emergency_stopped:
            return ApprovalDecision(approved=False, reason="emergency stop active for venture", auto=True)

        config = self._policies.get(req.venture_id, {}).get(req.action, _DEFAULT_POLICY_CONFIG)
        policy = config["policy"]

        get_event_bus().publish(
            AFOSEvent(
                type="approval.requested",
                source_agent="approval_engine",
                venture_id=req.venture_id,
                payload=req.model_dump(),
            )
        )

        if policy == ApprovalPolicy.EMERGENCY_STOP:
            return ApprovalDecision(approved=False, reason="policy is emergency_stop", auto=True)

        if policy == ApprovalPolicy.ALWAYS_APPROVE:
            return ApprovalDecision(approved=True, reason="policy: always_approve", auto=True)

        if policy == ApprovalPolicy.AUTO_APPROVE_UNDER_LIMIT:
            if req.amount_usd <= config["threshold_usd"]:
                return ApprovalDecision(
                    approved=True, reason=f"under threshold ${config['threshold_usd']}", auto=True
                )
            return self._ask_human(req)

        # risk_based
        if _RISK_ORDER[req.risk_level] < _RISK_ORDER[config["min_risk_for_approval"]]:
            return ApprovalDecision(
                approved=True, reason=f"risk '{req.risk_level}' below approval floor", auto=True
            )
        return self._ask_human(req)

    def _ask_human(self, req: ApprovalRequest) -> ApprovalDecision:
        human_response = interrupt(
            {
                "type": "approval_request",
                "action": req.action,
                "venture_id": req.venture_id,
                "risk_level": req.risk_level,
                "amount_usd": req.amount_usd,
                "details": req.details,
            }
        )
        if isinstance(human_response, dict):
            approved = bool(human_response.get("approved", False))
            reason = human_response.get("reason", "human decision")
        else:
            approved = bool(human_response)
            reason = "human decision"
        return ApprovalDecision(approved=approved, reason=reason, auto=False)


@lru_cache
def get_approval_engine() -> ApprovalPolicyEngine:
    from core.memory_gateway import get_memory_gateway

    return ApprovalPolicyEngine(get_memory_gateway().knowledge_conn)
