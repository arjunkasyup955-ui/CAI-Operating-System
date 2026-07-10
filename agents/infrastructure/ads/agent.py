import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp

import tools.ads.ads  # noqa: F401  (import registers the 15 ads tools)
from core.approval import ApprovalRequest, get_approval_engine
from core.event_bus import AFOSEvent, get_event_bus
from core.registries import get_agent_registry, get_tool_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.ads")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

# Reads (health check, get/list campaigns, analytics, audience lookup, keyword
# suggestions, ad preview) are low risk. Pausing is kept low - it's a safety/stop
# action, the same reasoning used for n8n's deactivate_workflow. Writes that create
# or update records (including resuming/budget changes) are medium - reversible,
# correctable, the same reasoning used for CRM's create/update operations. Deleting
# a campaign is high risk and genuinely requires human approval - destructive,
# hard-to-reverse, real ad-spend consequences, the same reasoning used for CRM's
# delete_lead and n8n's delete_workflow.
_RISK_LEVELS: dict[str, str] = {
    "ads_health_check": "low",
    "ads_create_campaign": "medium",
    "ads_update_campaign": "medium",
    "ads_pause_campaign": "low",
    "ads_resume_campaign": "medium",
    "ads_delete_campaign": "high",
    "ads_get_campaign": "low",
    "ads_list_campaigns": "low",
    "ads_campaign_analytics": "low",
    "ads_budget_update": "medium",
    "ads_audience_lookup": "low",
    "ads_keyword_suggestions": "low",
    "ads_ad_preview": "low",
    "ads_create_ad_group": "medium",
    "ads_create_ad": "medium",
}


def run_ads_operation(operation: str, venture_id: str = "default", **kwargs: Any) -> dict:
    """Core logic for every ads operation. Always goes through, in order: Event Bus
    (started), Approval Framework (risk-based gate), Tool Registry (schema
    validation + permission enforcement + retry, via the existing kernel
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
        AFOSEvent(type="ads_started", source_agent="ads_agent", venture_id=venture_id, payload={"operation": operation})
    )

    try:
        risk_level = _RISK_LEVELS.get(operation, "high")
        decision = get_approval_engine().request(
            ApprovalRequest(action=operation, venture_id=venture_id, risk_level=risk_level, details=kwargs)
        )
        if not decision.approved:
            entry = {"agent": "ads_agent", "event": "ads_failed", "operation": operation, "reason": decision.reason}
            bus.publish(
                AFOSEvent(type="ads_failed", source_agent="ads_agent", venture_id=venture_id, payload=entry)
            )
            return entry

        result = get_tool_registry().invoke(operation, agent_name="ads_agent", **kwargs)
        entry = {"agent": "ads_agent", "event": "ads_completed", "operation": operation, **result}
        bus.publish(
            AFOSEvent(type="ads_completed", source_agent="ads_agent", venture_id=venture_id, payload={"operation": operation})
        )
        return entry
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("ads_agent: operation '%s' failed: %s", operation, exc)
        entry = {"agent": "ads_agent", "event": "ads_failed", "operation": operation, "error": str(exc)}
        bus.publish(
            AFOSEvent(type="ads_failed", source_agent="ads_agent", venture_id=venture_id, payload=entry)
        )
        return entry


def ads_agent_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses the health check as the only
    zero-argument, side-effect-free operation available without requiring extra
    state fields that don't exist on VentureState yet.
    """
    entry = run_ads_operation("ads_health_check", venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
