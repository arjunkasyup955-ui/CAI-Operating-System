import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp

import tools.ollama.ollama  # noqa: F401  (import registers the 4 ollama tools)
from core.approval import ApprovalRequest, get_approval_engine
from core.event_bus import AFOSEvent, get_event_bus
from core.registries import get_agent_registry, get_tool_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.ollama_ops")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

_RISK_LEVELS: dict[str, str] = {
    "ollama_health_check": "low",
    "ollama_list_models": "low",
    "ollama_chat": "low",
    "ollama_stream_chat": "low",
}


def run_ollama_operation(operation: str, venture_id: str = "default", **kwargs: Any) -> dict:
    """Core logic for every Ollama operation. Always goes through, in order: Event Bus
    (started), Approval Framework (risk-based gate - all four operations are low risk:
    health/model-listing are read-only, and chat/stream either stay local and free or
    fall back through the Model Router's own existing chain), Tool Registry (schema
    validation + permission enforcement + retry, via the existing kernel RetryPolicy),
    Event Bus (completed/failed). Never raises for a genuine error.

    Important: the Approval Framework's high-risk path calls LangGraph's interrupt(),
    which raises GraphInterrupt (a subclass of Exception!) to pause the graph. A
    blanket `except Exception` would silently swallow that pause and break the gate
    entirely - GraphBubbleUp (its parent class) must always be re-raised, never
    treated as a failure. (Same fix already applied in every Phase 2 coding agent.)
    """
    bus = get_event_bus()

    bus.publish(
        AFOSEvent(type="ollama_started", source_agent="ollama_ops_agent", venture_id=venture_id, payload={"operation": operation})
    )

    try:
        risk_level = _RISK_LEVELS.get(operation, "high")
        decision = get_approval_engine().request(
            ApprovalRequest(action=operation, venture_id=venture_id, risk_level=risk_level, details=kwargs)
        )
        if not decision.approved:
            entry = {"agent": "ollama_ops_agent", "event": "ollama_failed", "operation": operation, "reason": decision.reason}
            bus.publish(
                AFOSEvent(type="ollama_failed", source_agent="ollama_ops_agent", venture_id=venture_id, payload=entry)
            )
            return entry

        result = get_tool_registry().invoke(operation, agent_name="ollama_ops_agent", **kwargs)
        entry = {"agent": "ollama_ops_agent", "event": "ollama_completed", "operation": operation, **result}
        bus.publish(
            AFOSEvent(type="ollama_completed", source_agent="ollama_ops_agent", venture_id=venture_id, payload={"operation": operation})
        )
        return entry
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("ollama_ops_agent: operation '%s' failed: %s", operation, exc)
        entry = {"agent": "ollama_ops_agent", "event": "ollama_failed", "operation": operation, "error": str(exc)}
        bus.publish(
            AFOSEvent(type="ollama_failed", source_agent="ollama_ops_agent", venture_id=venture_id, payload=entry)
        )
        return entry


def ollama_ops_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses the health check as the only
    zero-argument, side-effect-free operation available without requiring extra state
    fields that don't exist on VentureState yet.
    """
    entry = run_ollama_operation("ollama_health_check", venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
