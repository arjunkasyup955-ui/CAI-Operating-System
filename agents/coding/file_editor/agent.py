import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp

import tools.files.files  # noqa: F401  (import registers the 6 file tools)
from core.approval import ApprovalRequest, get_approval_engine
from core.event_bus import AFOSEvent, get_event_bus
from core.registries import get_agent_registry, get_tool_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.file_editor")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

# write_file can silently destroy existing content (it's create-or-overwrite), so it's
# rated high and genuinely pauses for human approval - the same reasoning that made
# Git's checkout high risk. create_file cannot overwrite (it fails if the file already
# exists), so it stays low.
_RISK_LEVELS: dict[str, str] = {
    "read_file": "low",
    "create_directory": "low",
    "create_file": "low",
    "append_file": "low",
    "replace_text": "medium",
    "write_file": "high",
}


def run_file_operation(operation: str, venture_id: str = "default", **kwargs: Any) -> dict:
    """Core logic for every file operation. Always goes through, in order: Event Bus
    (started), Approval Framework (risk-based gate), Tool Registry (schema validation
    + permission enforcement + retry, via the existing kernel RetryPolicy), Event Bus
    (completed/failed/denied). Never raises for a genuine error - a denied or failed
    operation is recorded and returned, not thrown.

    Important: the Approval Framework's high-risk path calls LangGraph's interrupt(),
    which raises GraphInterrupt (a subclass of Exception!) to pause the graph. A
    blanket `except Exception` would silently swallow that pause and break the gate
    entirely - GraphBubbleUp (its parent class) must always be re-raised, never
    treated as a failure. (Same fix already applied in the Git Agent.)
    """
    bus = get_event_bus()

    bus.publish(
        AFOSEvent(
            type="file_edit_started",
            source_agent="file_editor_agent",
            venture_id=venture_id,
            payload={"operation": operation, **kwargs},
        )
    )

    try:
        risk_level = _RISK_LEVELS.get(operation, "high")
        decision = get_approval_engine().request(
            ApprovalRequest(action=operation, venture_id=venture_id, risk_level=risk_level, details=kwargs)
        )
        if not decision.approved:
            entry = {
                "agent": "file_editor_agent",
                "event": "file_edit_denied",
                "operation": operation,
                "reason": decision.reason,
            }
            bus.publish(
                AFOSEvent(type="file_edit_denied", source_agent="file_editor_agent", venture_id=venture_id, payload=entry)
            )
            return entry

        result = get_tool_registry().invoke(operation, agent_name="file_editor_agent", **kwargs)
        entry = {"agent": "file_editor_agent", "event": "file_edit_completed", "operation": operation, **result}
        bus.publish(
            AFOSEvent(
                type="file_edit_completed",
                source_agent="file_editor_agent",
                venture_id=venture_id,
                payload={"operation": operation, "success": result.get("success")},
            )
        )
        return entry
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("file_editor_agent: operation '%s' failed: %s", operation, exc)
        entry = {"agent": "file_editor_agent", "event": "file_edit_failed", "operation": operation, "error": str(exc)}
        bus.publish(
            AFOSEvent(type="file_edit_failed", source_agent="file_editor_agent", venture_id=venture_id, payload=entry)
        )
        return entry


def file_editor_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses create_directory on a scratch
    path as the only zero-content, side-effect-light operation available without
    requiring extra state fields that don't exist on VentureState yet.
    """
    entry = run_file_operation(
        "create_directory", venture_id=state.get("venture_id", "default"), path="scratch/.afos_placeholder"
    )
    return {"history": [entry]}
