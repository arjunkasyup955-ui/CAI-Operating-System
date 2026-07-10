import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp

import tools.terminal.terminal  # noqa: F401  (import registers the terminal_execute tool)
from core.approval import ApprovalRequest, get_approval_engine
from core.event_bus import AFOSEvent, get_event_bus
from core.registries import get_agent_registry, get_tool_registry
from core.state import VentureState
from tools.terminal.terminal_providers import classify_risk, is_banned

logger = logging.getLogger("afos.agents.terminal")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")


def run_terminal_command(command: list[str], venture_id: str = "default", timeout_seconds: float = 30.0) -> dict:
    """Core logic for every terminal invocation. Always goes through, in order: Event
    Bus (started), a hard ban check (fails fast, never even asks for approval on a
    destructive command), Approval Framework (risk-based gate), Tool Registry (schema
    validation + permission enforcement + retry, via the existing kernel RetryPolicy),
    Event Bus (completed/failed). Never raises for a genuine error.

    Important: the Approval Framework's high-risk path calls LangGraph's interrupt(),
    which raises GraphInterrupt (a subclass of Exception!) to pause the graph. A
    blanket `except Exception` would silently swallow that pause and break the gate
    entirely - GraphBubbleUp (its parent class) must always be re-raised, never
    treated as a failure. (Same fix already applied in the Git and File Editor Agents.)
    """
    bus = get_event_bus()
    command_str = " ".join(command)

    bus.publish(
        AFOSEvent(
            type="terminal_started",
            source_agent="terminal_agent",
            venture_id=venture_id,
            payload={"command": command_str},
        )
    )

    try:
        if is_banned(command):
            entry: dict[str, Any] = {
                "agent": "terminal_agent",
                "event": "terminal_denied",
                "command": command_str,
                "reason": "command matches a banned destructive pattern",
            }
            bus.publish(
                AFOSEvent(type="terminal_failed", source_agent="terminal_agent", venture_id=venture_id, payload=entry)
            )
            return entry

        risk_level = classify_risk(command)
        decision = get_approval_engine().request(
            ApprovalRequest(
                action="terminal_execute", venture_id=venture_id, risk_level=risk_level, details={"command": command}
            )
        )
        if not decision.approved:
            entry = {
                "agent": "terminal_agent",
                "event": "terminal_denied",
                "command": command_str,
                "reason": decision.reason,
            }
            bus.publish(
                AFOSEvent(type="terminal_failed", source_agent="terminal_agent", venture_id=venture_id, payload=entry)
            )
            return entry

        result = get_tool_registry().invoke(
            "terminal_execute", agent_name="terminal_agent", command=command, timeout_seconds=timeout_seconds
        )
        entry = {"agent": "terminal_agent", "event": "terminal_completed", **result}
        bus.publish(
            AFOSEvent(
                type="terminal_completed",
                source_agent="terminal_agent",
                venture_id=venture_id,
                payload={"command": command_str, "success": result.get("success"), "exit_code": result.get("exit_code")},
            )
        )
        return entry
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("terminal_agent: command '%s' failed: %s", command_str, exc)
        entry = {"agent": "terminal_agent", "event": "terminal_failed", "command": command_str, "error": str(exc)}
        bus.publish(
            AFOSEvent(type="terminal_failed", source_agent="terminal_agent", venture_id=venture_id, payload=entry)
        )
        return entry


def terminal_agent_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses a version check as the only
    zero-risk, side-effect-free command available without requiring extra state
    fields that don't exist on VentureState yet.
    """
    import sys

    entry = run_terminal_command([sys.executable, "--version"], venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
