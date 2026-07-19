import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp
from pydantic import BaseModel

from core.registries import get_agent_registry
from core.state import VentureState
from workflows.founder_dashboard import compile_founder_dashboard

logger = logging.getLogger("afos.agents.founder_dashboard")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

_VALID_DEPTHS = {"standard", "deep"}


class VentureSummary(BaseModel):
    idea: str = ""
    venture_id: str = ""
    risk_level: str = "unknown"
    founder_status: str = "unknown"
    required_agents: list[str] = []


class ResearchSummary(BaseModel):
    status: str = "unknown"
    confidence_score: float = 0.0
    sources_count: int = 0
    research_depth: str = "standard"


class DecisionScores(BaseModel):
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
    recommendation: str = "unknown"


class MVPPlanSummary(BaseModel):
    status: str = "unknown"
    feature_count: int = 0
    tech_stack: list[dict[str, Any]] = []
    milestone_count: int = 0


class BuildStatus(BaseModel):
    status: str = "unknown"
    steps_completed: int = 0
    steps_total: int = 0
    debug_retry_invocations: int = 0


class DeploymentStatus(BaseModel):
    status: str = "unknown"
    ready_stage_count: int = 0
    total_stage_count: int = 0


class GrowthStatus(BaseModel):
    status: str = "unknown"
    ready_stage_count: int = 0
    total_stage_count: int = 0


class GitStatus(BaseModel):
    available: bool = False
    output: str = ""
    error: str = ""


class FounderDashboard(BaseModel):
    """The Founder Dashboard's single output contract - a unified digest of
    every prior Phase 4 component's already-computed results for one venture.
    Consumes nothing directly except those 7 components (plus the existing
    Phase 2 Git Agent for Git Status) - never calls a Tool Registry tool, an
    LLM, or a raw external API itself.
    """

    idea: str
    venture_id: str = ""
    status: str = "pending"
    overall_health: str = "unknown"
    venture_summary: VentureSummary = VentureSummary()
    research_summary: ResearchSummary = ResearchSummary()
    market_analysis: dict[str, Any] = {}
    competitor_summary: dict[str, Any] = {}
    research_data_quality: str = "unknown"
    decision_scores: DecisionScores = DecisionScores()
    mvp_plan: MVPPlanSummary = MVPPlanSummary()
    build_status: BuildStatus = BuildStatus()
    deployment_status: DeploymentStatus = DeploymentStatus()
    growth_status: GrowthStatus = GrowthStatus()
    git_status: GitStatus = GitStatus()
    alerts: list[str] = []
    risks: list[str] = []
    recommended_next_actions: list[str] = []
    summary: str = ""
    error: str = ""


def run_founder_dashboard(idea: str, venture_id: str, research_depth: str = "standard") -> dict:
    """Entry point for running the Founder Dashboard. Registered in the existing
    Agent Registry above; declares no permissions and calls the Tool Registry
    for nothing directly - it only ever reaches outside itself through the 7
    already-completed Phase 4 components and the existing, unmodified Phase 2
    Git Agent it aggregates (see workflows/founder_dashboard.py). Never raises
    for a genuine failure (GraphBubbleUp aside) - invalid input and any
    unexpected exception both degrade to a well-formed FounderDashboard with a
    descriptive status.

    Returns a dict shaped {"agent": ..., "event": ..., **FounderDashboard
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
        graph = compile_founder_dashboard()
        result = graph.invoke(initial_state)
        dashboard = FounderDashboard(**result.get("dashboard", {"idea": idea, "venture_id": venture_id, "status": "failed"}))
        return {"agent": "founder_dashboard", "event": "founder_dashboard_result", **dashboard.model_dump()}
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("founder_dashboard: unexpected pipeline failure for venture '%s': %s", venture_id, exc)
        return {
            "agent": "founder_dashboard",
            "event": "founder_dashboard_result",
            **FounderDashboard(idea=idea, venture_id=venture_id, status="failed", error=str(exc)).model_dump(),
        }


def founder_dashboard_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses idea/venture_id already
    present on VentureState - no extra state fields required.
    """
    entry = run_founder_dashboard(idea=state.get("idea", ""), venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
