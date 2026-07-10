import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp

import tools.docker.docker  # noqa: F401  (import registers the 11 docker tools)
from core.approval import ApprovalRequest, get_approval_engine
from core.event_bus import AFOSEvent, get_event_bus
from core.registries import get_agent_registry, get_tool_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.docker")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

_BASE_RISK_LEVELS: dict[str, str] = {
    "docker_health_check": "low",
    "docker_list_containers": "low",
    "docker_list_images": "low",
    "docker_pull_image": "medium",
    "docker_create_container": "medium",
    "docker_start_container": "medium",
    "docker_stop_container": "medium",
    "docker_restart_container": "medium",
    "docker_remove_container": "high",
    "docker_container_logs": "low",
    "docker_exec_command": "high",
}


def _classify_risk(operation: str, kwargs: dict[str, Any]) -> str:
    """Most operations have a fixed risk level, but a *forced* stop is as destructive
    as a kill and is escalated to high regardless of the operation's normal level -
    "all destructive operations (remove container, force stop, etc.) must require
    approval" applies to the force flag itself, not just the operation name.
    """
    if operation == "docker_stop_container" and kwargs.get("force"):
        return "high"
    return _BASE_RISK_LEVELS.get(operation, "high")


def run_docker_operation(operation: str, venture_id: str = "default", **kwargs: Any) -> dict:
    """Core logic for every Docker operation. Always goes through, in order: Event Bus
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
        AFOSEvent(type="docker_started", source_agent="docker_agent", venture_id=venture_id, payload={"operation": operation})
    )

    try:
        risk_level = _classify_risk(operation, kwargs)
        decision = get_approval_engine().request(
            ApprovalRequest(action=operation, venture_id=venture_id, risk_level=risk_level, details=kwargs)
        )
        if not decision.approved:
            entry = {"agent": "docker_agent", "event": "docker_failed", "operation": operation, "reason": decision.reason}
            bus.publish(
                AFOSEvent(type="docker_failed", source_agent="docker_agent", venture_id=venture_id, payload=entry)
            )
            return entry

        result = get_tool_registry().invoke(operation, agent_name="docker_agent", **kwargs)
        entry = {"agent": "docker_agent", "event": "docker_completed", "operation": operation, **result}
        bus.publish(
            AFOSEvent(type="docker_completed", source_agent="docker_agent", venture_id=venture_id, payload={"operation": operation})
        )
        return entry
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("docker_agent: operation '%s' failed: %s", operation, exc)
        entry = {"agent": "docker_agent", "event": "docker_failed", "operation": operation, "error": str(exc)}
        bus.publish(
            AFOSEvent(type="docker_failed", source_agent="docker_agent", venture_id=venture_id, payload=entry)
        )
        return entry


def docker_agent_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses the health check as the only
    zero-argument, side-effect-free operation available without requiring extra state
    fields that don't exist on VentureState yet.
    """
    entry = run_docker_operation("docker_health_check", venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
