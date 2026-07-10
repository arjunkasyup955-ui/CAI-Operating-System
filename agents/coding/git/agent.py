import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp

import tools.git.git  # noqa: F401  (import registers the 7 git tools)
from core.approval import ApprovalRequest, get_approval_engine
from core.event_bus import AFOSEvent, get_event_bus
from core.registries import get_agent_registry, get_tool_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.git")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

# Read-only/low-impact operations auto-approve under the default risk_based policy
# (min_risk_for_approval="high"); checkout can discard uncommitted work in the working
# tree, so it's rated high and genuinely pauses for human approval.
_RISK_LEVELS: dict[str, str] = {
    "status": "low",
    "diff": "low",
    "log": "low",
    "branch": "low",
    "add": "low",
    "commit": "medium",
    "checkout": "high",
}


def run_git_operation(operation: str, venture_id: str = "default", **kwargs: Any) -> dict:
    """Core logic for every git operation. Always goes through, in order: Event Bus
    (started), Approval Framework (risk-based gate), Tool Registry (schema validation
    + permission enforcement + retry, via the existing kernel RetryPolicy), Event Bus
    (completed/failed/denied). Never raises for a genuine error - a denied or failed
    operation is recorded and returned, not thrown.

    Important: the Approval Framework's high-risk path calls LangGraph's interrupt(),
    which raises GraphInterrupt (a subclass of Exception!) to pause the graph. A
    blanket `except Exception` would silently swallow that pause and break the gate
    entirely - GraphBubbleUp (its parent class) must always be re-raised, never
    treated as a failure.
    """
    bus = get_event_bus()
    tool_name = f"git_{operation}"

    bus.publish(
        AFOSEvent(
            type="git_operation_started",
            source_agent="git_agent",
            venture_id=venture_id,
            payload={"operation": operation, **kwargs},
        )
    )

    try:
        risk_level = _RISK_LEVELS.get(operation, "high")
        decision = get_approval_engine().request(
            ApprovalRequest(action=tool_name, venture_id=venture_id, risk_level=risk_level, details=kwargs)
        )
        if not decision.approved:
            entry = {
                "agent": "git_agent",
                "event": "git_operation_denied",
                "operation": operation,
                "reason": decision.reason,
            }
            bus.publish(
                AFOSEvent(type="git_operation_failed", source_agent="git_agent", venture_id=venture_id, payload=entry)
            )
            return entry

        result = get_tool_registry().invoke(tool_name, agent_name="git_agent", **kwargs)
        entry = {"agent": "git_agent", "event": "git_operation_completed", "operation": operation, **result}
        bus.publish(
            AFOSEvent(
                type="git_operation_completed",
                source_agent="git_agent",
                venture_id=venture_id,
                payload={"operation": operation, "success": result.get("success")},
            )
        )
        return entry
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("git_agent: operation '%s' failed: %s", operation, exc)
        entry = {"agent": "git_agent", "event": "git_operation_failed", "operation": operation, "error": str(exc)}
        bus.publish(
            AFOSEvent(type="git_operation_failed", source_agent="git_agent", venture_id=venture_id, payload=entry)
        )
        return entry


def git_agent_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component - a status check is the only
    zero-argument, side-effect-free operation, used here purely to keep this wrapper
    directly testable/callable without requiring extra state fields that don't exist
    on VentureState yet.
    """
    entry = run_git_operation("status", venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
