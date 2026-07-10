import logging
from pathlib import Path

from core.approval import ApprovalRequest, get_approval_engine
from core.registries import get_agent_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.manager")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")


def manager_node(state: VentureState) -> dict:
    """Phase 0 stub: no real routing logic yet, just proves the graph carries state
    and appends to shared history. Domain-supervisor routing arrives with Phase 1.
    """
    idea = state.get("idea", "")
    logger.info("manager received idea: %s", idea)
    return {
        "phase": "research",
        "history": [{"agent": "manager", "event": "received_idea", "idea": idea}],
    }


def approval_gate_node(state: VentureState) -> dict:
    """Proves the Human Approval Framework: risk_based policy at 'high' risk pauses
    the graph via interrupt() until a human resumes it with a decision.
    """
    decision = get_approval_engine().request(
        ApprovalRequest(
            action="advance_to_research",
            venture_id=state["venture_id"],
            risk_level="high",
            details={"idea": state.get("idea", "")},
        )
    )
    return {
        "history": [
            {
                "agent": "manager",
                "event": "approval_decision",
                "approved": decision.approved,
                "reason": decision.reason,
            }
        ],
    }
