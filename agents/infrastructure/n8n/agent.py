import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp

import tools.n8n.n8n  # noqa: F401  (import registers the 10 n8n tools)
from core.approval import ApprovalRequest, get_approval_engine
from core.event_bus import AFOSEvent, get_event_bus
from core.registries import get_agent_registry, get_tool_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.n8n")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

# Reads are low risk; creating/updating/activating a workflow is medium (reversible);
# deleting a workflow (explicitly required) and executing one (which can trigger
# arbitrary real-world side effects depending on what the workflow does) are high and
# genuinely require approval. Deactivating is kept low - it's a safety action, the
# same reasoning used for rollback/stop operations in prior components.
_RISK_LEVELS: dict[str, str] = {
    "n8n_health_check": "low",
    "n8n_list_workflows": "low",
    "n8n_get_workflow": "low",
    "n8n_create_workflow": "medium",
    "n8n_update_workflow": "medium",
    "n8n_activate_workflow": "medium",
    "n8n_deactivate_workflow": "low",
    "n8n_delete_workflow": "high",
    "n8n_execute_workflow": "high",
    "n8n_workflow_executions": "low",
}


def run_n8n_operation(operation: str, venture_id: str = "default", **kwargs: Any) -> dict:
    """Core logic for every n8n operation. Always goes through, in order: Event Bus
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
        AFOSEvent(type="n8n_started", source_agent="n8n_agent", venture_id=venture_id, payload={"operation": operation})
    )

    try:
        risk_level = _RISK_LEVELS.get(operation, "high")
        decision = get_approval_engine().request(
            ApprovalRequest(action=operation, venture_id=venture_id, risk_level=risk_level, details=kwargs)
        )
        if not decision.approved:
            entry = {"agent": "n8n_agent", "event": "n8n_failed", "operation": operation, "reason": decision.reason}
            bus.publish(
                AFOSEvent(type="n8n_failed", source_agent="n8n_agent", venture_id=venture_id, payload=entry)
            )
            return entry

        result = get_tool_registry().invoke(operation, agent_name="n8n_agent", **kwargs)
        entry = {"agent": "n8n_agent", "event": "n8n_completed", "operation": operation, **result}
        bus.publish(
            AFOSEvent(type="n8n_completed", source_agent="n8n_agent", venture_id=venture_id, payload={"operation": operation})
        )
        return entry
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("n8n_agent: operation '%s' failed: %s", operation, exc)
        entry = {"agent": "n8n_agent", "event": "n8n_failed", "operation": operation, "error": str(exc)}
        bus.publish(
            AFOSEvent(type="n8n_failed", source_agent="n8n_agent", venture_id=venture_id, payload=entry)
        )
        return entry


def n8n_agent_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses the health check as the only
    zero-argument, side-effect-free operation available without requiring extra state
    fields that don't exist on VentureState yet.
    """
    entry = run_n8n_operation("n8n_health_check", venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
