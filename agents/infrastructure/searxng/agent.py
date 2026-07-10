import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp

import tools.searxng.searxng  # noqa: F401  (import registers the 4 searxng tools)
from core.approval import ApprovalRequest, get_approval_engine
from core.event_bus import AFOSEvent, get_event_bus
from core.registries import get_agent_registry, get_tool_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.searxng")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

# Every operation here is a read-only search against public/self-hosted SearXNG
# indices - none of them mutate any external system, so all are low risk. They
# still route through the Approval Framework on every call (never bypassed, per the
# frozen kernel's contract), it just always auto-approves at low risk - the same
# reasoning applied to the read-only operations in the Crawl4AI component.
_RISK_LEVELS: dict[str, str] = {
    "searxng_health_check": "low",
    "searxng_web_search": "low",
    "searxng_news_search": "low",
    "searxng_image_search": "low",
}


def run_searxng_operation(operation: str, venture_id: str = "default", **kwargs: Any) -> dict:
    """Core logic for every searxng operation. Always goes through, in order: Event
    Bus (started), Approval Framework (risk-based gate), Tool Registry (schema
    validation + permission enforcement + retry, via the existing kernel
    RetryPolicy), Event Bus (completed/failed). Never raises for a genuine error.

    Important: the Approval Framework's high-risk path calls LangGraph's
    interrupt(), which raises GraphInterrupt (a subclass of Exception!) to pause
    the graph. A blanket `except Exception` would silently swallow that pause and
    break the gate entirely - GraphBubbleUp (its parent class) must always be
    re-raised, never treated as a failure. (Same fix already applied in every prior
    Phase 2/3 agent, even though every operation here resolves to low risk and
    never actually interrupts.)
    """
    bus = get_event_bus()

    bus.publish(
        AFOSEvent(type="searxng_started", source_agent="searxng_agent", venture_id=venture_id, payload={"operation": operation})
    )

    try:
        risk_level = _RISK_LEVELS.get(operation, "high")
        decision = get_approval_engine().request(
            ApprovalRequest(action=operation, venture_id=venture_id, risk_level=risk_level, details=kwargs)
        )
        if not decision.approved:
            entry = {"agent": "searxng_agent", "event": "searxng_failed", "operation": operation, "reason": decision.reason}
            bus.publish(
                AFOSEvent(type="searxng_failed", source_agent="searxng_agent", venture_id=venture_id, payload=entry)
            )
            return entry

        result = get_tool_registry().invoke(operation, agent_name="searxng_agent", **kwargs)
        entry = {"agent": "searxng_agent", "event": "searxng_completed", "operation": operation, **result}
        bus.publish(
            AFOSEvent(type="searxng_completed", source_agent="searxng_agent", venture_id=venture_id, payload={"operation": operation})
        )
        return entry
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("searxng_agent: operation '%s' failed: %s", operation, exc)
        entry = {"agent": "searxng_agent", "event": "searxng_failed", "operation": operation, "error": str(exc)}
        bus.publish(
            AFOSEvent(type="searxng_failed", source_agent="searxng_agent", venture_id=venture_id, payload=entry)
        )
        return entry


def searxng_agent_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses the health check as the only
    zero-argument, side-effect-free operation available without requiring extra
    state fields that don't exist on VentureState yet.
    """
    entry = run_searxng_operation("searxng_health_check", venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
