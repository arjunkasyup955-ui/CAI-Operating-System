import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp

import tools.database.postgres  # noqa: F401  (import registers the 10 postgres tools)
from core.approval import ApprovalRequest, get_approval_engine
from core.event_bus import AFOSEvent, get_event_bus
from core.registries import get_agent_registry, get_tool_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.postgres")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

# Reads are low risk; writes that only add/inspect data are medium; anything that can
# destroy data (delete) or affect the whole server (create_database) is high and
# genuinely requires approval. Rolling back a transaction is itself a safety action,
# so it's kept low - never gating the one operation meant to undo a mistake.
_RISK_LEVELS: dict[str, str] = {
    "postgres_health_check": "low",
    "postgres_create_database": "high",
    "postgres_create_table": "medium",
    "postgres_select": "low",
    "postgres_insert": "medium",
    "postgres_update": "medium",
    "postgres_delete": "high",
    "postgres_begin_transaction": "low",
    "postgres_commit_transaction": "low",
    "postgres_rollback_transaction": "low",
}


def run_postgres_operation(operation: str, venture_id: str = "default", **kwargs: Any) -> dict:
    """Core logic for every PostgreSQL operation. Always goes through, in order: Event
    Bus (started), Approval Framework (risk-based gate), Tool Registry (schema
    validation + permission enforcement + retry, via the existing kernel RetryPolicy),
    Event Bus (completed/failed). Never raises for a genuine error.

    Important: the Approval Framework's high-risk path calls LangGraph's interrupt(),
    which raises GraphInterrupt (a subclass of Exception!) to pause the graph. A
    blanket `except Exception` would silently swallow that pause and break the gate
    entirely - GraphBubbleUp (its parent class) must always be re-raised, never
    treated as a failure. (Same fix already applied in every Phase 2 coding agent and
    the Ollama Ops Agent.)
    """
    bus = get_event_bus()

    bus.publish(
        AFOSEvent(type="postgres_started", source_agent="postgres_agent", venture_id=venture_id, payload={"operation": operation})
    )

    try:
        risk_level = _RISK_LEVELS.get(operation, "high")
        decision = get_approval_engine().request(
            ApprovalRequest(action=operation, venture_id=venture_id, risk_level=risk_level, details=kwargs)
        )
        if not decision.approved:
            entry = {"agent": "postgres_agent", "event": "postgres_failed", "operation": operation, "reason": decision.reason}
            bus.publish(
                AFOSEvent(type="postgres_failed", source_agent="postgres_agent", venture_id=venture_id, payload=entry)
            )
            return entry

        result = get_tool_registry().invoke(operation, agent_name="postgres_agent", **kwargs)
        entry = {"agent": "postgres_agent", "event": "postgres_completed", "operation": operation, **result}
        bus.publish(
            AFOSEvent(type="postgres_completed", source_agent="postgres_agent", venture_id=venture_id, payload={"operation": operation})
        )
        return entry
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("postgres_agent: operation '%s' failed: %s", operation, exc)
        entry = {"agent": "postgres_agent", "event": "postgres_failed", "operation": operation, "error": str(exc)}
        bus.publish(
            AFOSEvent(type="postgres_failed", source_agent="postgres_agent", venture_id=venture_id, payload=entry)
        )
        return entry


def postgres_agent_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses the health check as the only
    zero-argument, side-effect-free operation available without requiring extra state
    fields that don't exist on VentureState yet.
    """
    entry = run_postgres_operation("postgres_health_check", venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
