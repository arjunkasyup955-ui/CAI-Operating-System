import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp
from pydantic import BaseModel

from core.registries import get_agent_registry  # noqa: I001 (must precede core.permissions - see note below)
from core.state import VentureState
from workflows.decision_engine import compile_decision_engine

# core.registries must be imported before core.permissions in modules that touch
# both - a pre-existing kernel import-order fragility (core.registries.
# tool_registry imports core.permissions.PermissionDeniedError at module load
# time). This module doesn't import core.permissions at all (the manifest below
# declares no permissions - decision_engine never calls the Tool Registry), so
# the ordering constraint doesn't actually bite here, but the import of
# core.registries is kept first regardless for consistency with every other
# Phase 4 component's documented note on this.

logger = logging.getLogger("afos.agents.decision_engine")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

_VALID_DEPTHS = {"standard", "deep"}


class FounderDecisionReport(BaseModel):
    """The Decision Engine's single output contract - a deterministic, pure-Python
    scorecard derived entirely from the already-completed Research Pipeline's
    FounderResearchReport. Every score is 0-10, where higher is always more
    favorable to building the venture.
    """

    idea: str
    opportunity_score: float = 0.0
    competition_score: float = 0.0
    risk_score: float = 0.0
    market_size_score: float = 0.0
    build_difficulty: float = 0.0
    execution_complexity: float = 0.0
    revenue_potential: float = 0.0
    market_timing: float = 0.0
    ai_advantage: float = 0.0
    confidence_score: float = 0.0
    overall_score: float = 0.0
    recommendation: str = "DROP"
    reasons: list[str] = []
    warnings: list[str] = []
    next_actions: list[str] = []
    status: str = "pending"


def run_decision_engine(idea: str, venture_id: str, research_depth: str = "standard") -> dict:
    """Entry point for running the Decision Engine. Registered in the existing
    Agent Registry above; declares no permissions and calls the Tool Registry for
    nothing - it only ever reaches outside itself through the already-completed
    Research Pipeline (see workflows/decision_engine.py's dependency-injected
    research pipeline invoker). Never raises for a genuine failure (GraphBubbleUp
    aside) - invalid input and unexpected exceptions both degrade to a
    well-formed FounderDecisionReport with status "rejected"/"failed".

    Returns a dict shaped {"agent": ..., "event": ..., **FounderDecisionReport
    fields}.
    """
    depth = research_depth if research_depth in _VALID_DEPTHS else "standard"

    initial_state: dict[str, Any] = {
        "idea": idea,
        "venture_id": venture_id,
        "phase": "decision",
        "research_depth": depth,
    }

    try:
        graph = compile_decision_engine()
        result = graph.invoke(initial_state)
        report = FounderDecisionReport(**result.get("report", {"idea": idea, "status": "failed"}))
        return {"agent": "decision_engine", "event": "decision_engine_result", **report.model_dump()}
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("decision_engine: unexpected pipeline failure for venture '%s': %s", venture_id, exc)
        return {
            "agent": "decision_engine",
            "event": "decision_engine_result",
            **FounderDecisionReport(idea=idea, status="failed", warnings=[str(exc)]).model_dump(),
        }


def decision_engine_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses idea/venture_id already
    present on VentureState - no extra state fields required.
    """
    entry = run_decision_engine(idea=state.get("idea", ""), venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
