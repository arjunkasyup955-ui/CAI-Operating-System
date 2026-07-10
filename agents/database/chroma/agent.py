import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp

import tools.chroma.chroma  # noqa: F401  (import registers the 8 chroma tools)
from core.approval import ApprovalRequest, get_approval_engine
from core.event_bus import AFOSEvent, get_event_bus
from core.registries import get_agent_registry, get_tool_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.chroma")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

# Reads (health/list/search) are low risk; creating a collection or adding/updating
# documents is medium (additive, reversible); deleting a whole collection or specific
# documents is high (data loss) and genuinely requires approval.
_RISK_LEVELS: dict[str, str] = {
    "chroma_health_check": "low",
    "chroma_create_collection": "medium",
    "chroma_delete_collection": "high",
    "chroma_list_collections": "low",
    "chroma_add_documents": "medium",
    "chroma_update_documents": "medium",
    "chroma_delete_documents": "high",
    "chroma_similarity_search": "low",
}


def run_chroma_operation(operation: str, venture_id: str = "default", **kwargs: Any) -> dict:
    """Core logic for every Chroma operation. Always goes through, in order: Event Bus
    (started), Approval Framework (risk-based gate), Tool Registry (schema validation
    + permission enforcement + retry, via the existing kernel RetryPolicy), Event Bus
    (completed/failed). Never raises for a genuine error.

    Important: the Approval Framework's high-risk path calls LangGraph's interrupt(),
    which raises GraphInterrupt (a subclass of Exception!) to pause the graph. A
    blanket `except Exception` would silently swallow that pause and break the gate
    entirely - GraphBubbleUp (its parent class) must always be re-raised, never
    treated as a failure. (Same fix already applied in every prior Phase 2/3 agent.)
    """
    bus = get_event_bus()

    bus.publish(
        AFOSEvent(type="chroma_started", source_agent="chroma_agent", venture_id=venture_id, payload={"operation": operation})
    )

    try:
        risk_level = _RISK_LEVELS.get(operation, "high")
        decision = get_approval_engine().request(
            ApprovalRequest(action=operation, venture_id=venture_id, risk_level=risk_level, details=kwargs)
        )
        if not decision.approved:
            entry = {"agent": "chroma_agent", "event": "chroma_failed", "operation": operation, "reason": decision.reason}
            bus.publish(
                AFOSEvent(type="chroma_failed", source_agent="chroma_agent", venture_id=venture_id, payload=entry)
            )
            return entry

        result = get_tool_registry().invoke(operation, agent_name="chroma_agent", **kwargs)
        entry = {"agent": "chroma_agent", "event": "chroma_completed", "operation": operation, **result}
        bus.publish(
            AFOSEvent(type="chroma_completed", source_agent="chroma_agent", venture_id=venture_id, payload={"operation": operation})
        )
        return entry
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("chroma_agent: operation '%s' failed: %s", operation, exc)
        entry = {"agent": "chroma_agent", "event": "chroma_failed", "operation": operation, "error": str(exc)}
        bus.publish(
            AFOSEvent(type="chroma_failed", source_agent="chroma_agent", venture_id=venture_id, payload=entry)
        )
        return entry


def chroma_agent_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses the health check as the only
    zero-argument, side-effect-free operation available without requiring extra state
    fields that don't exist on VentureState yet.
    """
    entry = run_chroma_operation("chroma_health_check", venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
