import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp

import tools.playwright.playwright  # noqa: F401  (import registers the 11 playwright tools)
from core.approval import ApprovalRequest, get_approval_engine
from core.event_bus import AFOSEvent, get_event_bus
from core.registries import get_agent_registry, get_tool_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.playwright")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

# Reads (health check, extraction, screenshot, waiting) are low risk; launching a
# browser is medium (consumes real resources); form filling and clicking are medium
# (real side effects on whatever site is loaded, but scoped to declared
# selectors/values); closing a browser is kept low - it's a cleanup/safety action,
# the same reasoning used for deactivate/stop operations in prior components.
# Executing arbitrary JavaScript is explicitly required to be high risk - it can do
# anything the page's JS context allows.
_RISK_LEVELS: dict[str, str] = {
    "playwright_health_check": "low",
    "playwright_launch_browser": "medium",
    "playwright_close_browser": "low",
    "playwright_open_url": "low",
    "playwright_screenshot": "low",
    "playwright_extract_html": "low",
    "playwright_extract_text": "low",
    "playwright_execute_javascript": "high",
    "playwright_fill_form": "medium",
    "playwright_click_element": "medium",
    "playwright_wait_for_selector": "low",
}


def run_playwright_operation(operation: str, venture_id: str = "default", **kwargs: Any) -> dict:
    """Core logic for every playwright operation. Always goes through, in order:
    Event Bus (started), Approval Framework (risk-based gate), Tool Registry
    (schema validation + permission enforcement + retry, via the existing kernel
    RetryPolicy), Event Bus (completed/failed). Never raises for a genuine error.

    Important: the Approval Framework's high-risk path calls LangGraph's
    interrupt(), which raises GraphInterrupt (a subclass of Exception!) to pause
    the graph. A blanket `except Exception` would silently swallow that pause and
    break the gate entirely - GraphBubbleUp (its parent class) must always be
    re-raised, never treated as a failure. (Same fix already applied in every prior
    Phase 2/3 agent.)
    """
    bus = get_event_bus()

    bus.publish(
        AFOSEvent(type="playwright_started", source_agent="playwright_agent", venture_id=venture_id, payload={"operation": operation})
    )

    try:
        risk_level = _RISK_LEVELS.get(operation, "high")
        decision = get_approval_engine().request(
            ApprovalRequest(action=operation, venture_id=venture_id, risk_level=risk_level, details=kwargs)
        )
        if not decision.approved:
            entry = {"agent": "playwright_agent", "event": "playwright_failed", "operation": operation, "reason": decision.reason}
            bus.publish(
                AFOSEvent(type="playwright_failed", source_agent="playwright_agent", venture_id=venture_id, payload=entry)
            )
            return entry

        result = get_tool_registry().invoke(operation, agent_name="playwright_agent", **kwargs)
        entry = {"agent": "playwright_agent", "event": "playwright_completed", "operation": operation, **result}
        bus.publish(
            AFOSEvent(type="playwright_completed", source_agent="playwright_agent", venture_id=venture_id, payload={"operation": operation})
        )
        return entry
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("playwright_agent: operation '%s' failed: %s", operation, exc)
        entry = {"agent": "playwright_agent", "event": "playwright_failed", "operation": operation, "error": str(exc)}
        bus.publish(
            AFOSEvent(type="playwright_failed", source_agent="playwright_agent", venture_id=venture_id, payload=entry)
        )
        return entry


def playwright_agent_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses the health check as the only
    zero-argument, side-effect-free operation available without requiring extra
    state fields that don't exist on VentureState yet.
    """
    entry = run_playwright_operation("playwright_health_check", venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
