import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp
from pydantic import BaseModel

from core.registries import get_agent_registry
from core.state import VentureState
from workflows.mvp_planner import compile_mvp_planner

logger = logging.getLogger("afos.agents.mvp_planner")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

_VALID_DEPTHS = {"standard", "deep"}


class PRD(BaseModel):
    title: str = ""
    problem_statement: str = ""
    target_users: list[str] = []
    goals: list[str] = []
    success_metrics: list[str] = []
    scope_summary: str = ""


class MVPScope(BaseModel):
    included: list[str] = []
    excluded: list[str] = []
    rationale: str = ""


class Feature(BaseModel):
    name: str
    description: str = ""
    priority: str = "should_have"


class TechStackItem(BaseModel):
    layer: str
    technology: str
    rationale: str = ""


class DatabaseTable(BaseModel):
    name: str
    columns: list[str] = []
    purpose: str = ""


class APIEndpoint(BaseModel):
    method: str
    path: str
    description: str = ""


class TimelinePhase(BaseModel):
    phase: str
    weeks: str
    tasks: list[str] = []


class Milestone(BaseModel):
    name: str
    target_week: int
    deliverable: str = ""


class RiskItem(BaseModel):
    risk: str
    severity: str = "medium"
    mitigation: str = ""


class MVPPlan(BaseModel):
    """The MVP Planner's single output contract - a deterministic, pure-Python
    build plan derived entirely from the already-completed Founder Decision
    Engine's FounderDecisionReport.
    """

    idea: str
    prd: PRD = PRD()
    mvp_scope: MVPScope = MVPScope()
    feature_list: list[Feature] = []
    tech_stack: list[TechStackItem] = []
    database_outline: list[DatabaseTable] = []
    api_outline: list[APIEndpoint] = []
    folder_structure: list[str] = []
    development_timeline: list[TimelinePhase] = []
    milestones: list[Milestone] = []
    risks_and_dependencies: list[RiskItem] = []
    status: str = "pending"


def run_mvp_planner(idea: str, venture_id: str, research_depth: str = "standard") -> dict:
    """Entry point for running the MVP Planner. Registered in the existing Agent
    Registry above; declares no permissions and calls the Tool Registry for
    nothing - it only ever reaches outside itself through the already-completed
    Founder Decision Engine (see workflows/mvp_planner.py's dependency-injected
    decision engine invoker). Never raises for a genuine failure (GraphBubbleUp
    aside) - invalid input, a failed/incomplete decision, and a PIVOT/DROP
    recommendation all degrade to a well-formed MVPPlan with a descriptive status.

    Returns a dict shaped {"agent": ..., "event": ..., **MVPPlan fields}.
    """
    depth = research_depth if research_depth in _VALID_DEPTHS else "standard"

    initial_state: dict[str, Any] = {
        "idea": idea,
        "venture_id": venture_id,
        "phase": "product",
        "research_depth": depth,
    }

    try:
        graph = compile_mvp_planner()
        result = graph.invoke(initial_state)
        plan = MVPPlan(**result.get("plan", {"idea": idea, "status": "failed"}))
        return {"agent": "mvp_planner", "event": "mvp_planner_result", **plan.model_dump()}
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("mvp_planner: unexpected pipeline failure for venture '%s': %s", venture_id, exc)
        return {
            "agent": "mvp_planner",
            "event": "mvp_planner_result",
            **MVPPlan(idea=idea, status="failed").model_dump(),
        }


def mvp_planner_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses idea/venture_id already
    present on VentureState - no extra state fields required.
    """
    entry = run_mvp_planner(idea=state.get("idea", ""), venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
