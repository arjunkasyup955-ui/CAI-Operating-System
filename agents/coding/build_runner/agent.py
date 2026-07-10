import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp

import tools.build.build  # noqa: F401  (import registers the 3 build tools)
from core.approval import ApprovalRequest, get_approval_engine
from core.event_bus import AFOSEvent, get_event_bus
from core.registries import get_agent_registry, get_tool_registry
from core.state import VentureState
from tools.build.build_providers import build_node_args, build_python_args, classify_build_risk
from tools.terminal.terminal_providers import is_banned

logger = logging.getLogger("afos.agents.build_runner")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

_TOOL_NAMES = {"python": "build_python", "node": "build_node", "generic": "build_generic"}


def _resolve_args_for_risk_check(build_type: str, kwargs: dict[str, Any]) -> list[str]:
    """Builds the same argv the tool itself will construct, purely so risk
    classification and the destructive-command ban can run *before* approval is
    requested - never executes anything here.
    """
    if build_type == "python":
        return build_python_args(kwargs.get("action", ""), kwargs.get("extra_args"))
    if build_type == "node":
        return build_node_args(kwargs.get("action", ""), kwargs.get("extra_args"))
    return list(kwargs.get("command", []))


def run_build_operation(build_type: str, venture_id: str = "default", **kwargs: Any) -> dict:
    """Core logic for every build invocation. Always goes through, in order: Event Bus
    (started), a hard ban check (fails fast, never even asks for approval on a
    destructive command), Approval Framework (risk-based gate), Tool Registry (schema
    validation + permission enforcement + retry, via the existing kernel RetryPolicy),
    Event Bus (completed/failed). Never raises for a genuine error.

    Important: the Approval Framework's high-risk path calls LangGraph's interrupt(),
    which raises GraphInterrupt (a subclass of Exception!) to pause the graph. A
    blanket `except Exception` would silently swallow that pause and break the gate
    entirely - GraphBubbleUp (its parent class) must always be re-raised, never
    treated as a failure. (Same fix already applied in the Git, File Editor, and
    Terminal Agents.)
    """
    bus = get_event_bus()
    tool_name = _TOOL_NAMES.get(build_type)
    if tool_name is None:
        raise ValueError(f"unknown build_type: {build_type}")

    bus.publish(
        AFOSEvent(
            type="build_started",
            source_agent="build_runner_agent",
            venture_id=venture_id,
            payload={"build_type": build_type, **kwargs},
        )
    )

    try:
        args = _resolve_args_for_risk_check(build_type, kwargs)

        if is_banned(args):
            entry: dict[str, Any] = {
                "agent": "build_runner_agent",
                "event": "build_denied",
                "build_type": build_type,
                "reason": "command matches a banned destructive pattern",
            }
            bus.publish(
                AFOSEvent(type="build_failed", source_agent="build_runner_agent", venture_id=venture_id, payload=entry)
            )
            return entry

        risk_level = classify_build_risk(kwargs.get("action", build_type), args)
        decision = get_approval_engine().request(
            ApprovalRequest(action=tool_name, venture_id=venture_id, risk_level=risk_level, details=kwargs)
        )
        if not decision.approved:
            entry = {
                "agent": "build_runner_agent",
                "event": "build_denied",
                "build_type": build_type,
                "reason": decision.reason,
            }
            bus.publish(
                AFOSEvent(type="build_failed", source_agent="build_runner_agent", venture_id=venture_id, payload=entry)
            )
            return entry

        result = get_tool_registry().invoke(tool_name, agent_name="build_runner_agent", **kwargs)
        entry = {"agent": "build_runner_agent", "event": "build_completed", "build_type": build_type, **result}
        bus.publish(
            AFOSEvent(
                type="build_completed",
                source_agent="build_runner_agent",
                venture_id=venture_id,
                payload={"build_type": build_type, "success": result.get("success"), "status": result.get("status")},
            )
        )
        return entry
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("build_runner_agent: build_type '%s' failed: %s", build_type, exc)
        entry = {"agent": "build_runner_agent", "event": "build_failed", "build_type": build_type, "error": str(exc)}
        bus.publish(
            AFOSEvent(type="build_failed", source_agent="build_runner_agent", venture_id=venture_id, payload=entry)
        )
        return entry


def build_runner_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses the Python test workflow (low
    risk, auto-approved) as the only zero-side-effect action available without
    requiring extra state fields that don't exist on VentureState yet.
    """
    entry = run_build_operation("python", venture_id=state.get("venture_id", "default"), action="test")
    return {"history": [entry]}
