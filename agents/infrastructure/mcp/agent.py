import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp

import tools.mcp.mcp  # noqa: F401  (import registers the 8 mcp tools)
from core.approval import ApprovalRequest, get_approval_engine
from core.event_bus import AFOSEvent, get_event_bus
from core.registries import get_agent_registry, get_tool_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.mcp")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

# Reads (health check, list servers/tools, resource read, prompt execution) and
# session bookkeeping (connect/disconnect) are low risk. Calling a remote tool is
# kept medium - the tool lives on a server we don't control, so its side effects
# are unknown, the same reasoning used for Playwright's fill_form/click_element.
# Nothing here is escalated to high/approval-interrupt territory since no operation
# was called out as requiring it (unlike n8n's delete_workflow or Playwright's
# execute_javascript).
_RISK_LEVELS: dict[str, str] = {
    "mcp_health_check": "low",
    "mcp_list_servers": "low",
    "mcp_connect_server": "low",
    "mcp_disconnect_server": "low",
    "mcp_list_tools": "low",
    "mcp_call_tool": "medium",
    "mcp_read_resource": "low",
    "mcp_execute_prompt": "low",
}


def run_mcp_operation(operation: str, venture_id: str = "default", **kwargs: Any) -> dict:
    """Core logic for every MCP operation. Always goes through, in order: Event Bus
    (started), Approval Framework (risk-based gate), Tool Registry (schema
    validation + permission enforcement + retry, via the existing kernel
    RetryPolicy), Event Bus (completed/failed). Never raises for a genuine error.

    Important: the Approval Framework's high-risk path calls LangGraph's
    interrupt(), which raises GraphInterrupt (a subclass of Exception!) to pause
    the graph. A blanket `except Exception` would silently swallow that pause and
    break the gate entirely - GraphBubbleUp (its parent class) must always be
    re-raised, never treated as a failure. (Same fix already applied in every prior
    Phase 2/3 agent, even though every operation here resolves to low/medium risk
    and never actually interrupts under the default risk_based policy.)
    """
    bus = get_event_bus()

    bus.publish(
        AFOSEvent(type="mcp_started", source_agent="mcp_agent", venture_id=venture_id, payload={"operation": operation})
    )

    try:
        risk_level = _RISK_LEVELS.get(operation, "high")
        decision = get_approval_engine().request(
            ApprovalRequest(action=operation, venture_id=venture_id, risk_level=risk_level, details=kwargs)
        )
        if not decision.approved:
            entry = {"agent": "mcp_agent", "event": "mcp_failed", "operation": operation, "reason": decision.reason}
            bus.publish(
                AFOSEvent(type="mcp_failed", source_agent="mcp_agent", venture_id=venture_id, payload=entry)
            )
            return entry

        result = get_tool_registry().invoke(operation, agent_name="mcp_agent", **kwargs)
        entry = {"agent": "mcp_agent", "event": "mcp_completed", "operation": operation, **result}
        bus.publish(
            AFOSEvent(type="mcp_completed", source_agent="mcp_agent", venture_id=venture_id, payload={"operation": operation})
        )
        return entry
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("mcp_agent: operation '%s' failed: %s", operation, exc)
        entry = {"agent": "mcp_agent", "event": "mcp_failed", "operation": operation, "error": str(exc)}
        bus.publish(
            AFOSEvent(type="mcp_failed", source_agent="mcp_agent", venture_id=venture_id, payload=entry)
        )
        return entry


def mcp_agent_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses the health check as the only
    zero-argument, side-effect-free operation available without requiring extra
    state fields that don't exist on VentureState yet.
    """
    entry = run_mcp_operation("mcp_health_check", venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
