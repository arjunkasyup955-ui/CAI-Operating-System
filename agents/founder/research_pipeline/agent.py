import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp
from pydantic import BaseModel

from core.registries import get_agent_registry  # noqa: I001 (must precede core.permissions - see note below)
from core.permissions import Permission, get_permission_gate
from core.state import VentureState
from workflows.research_pipeline import _STAGE_AGENT_NAMES, compile_research_pipeline

# core.registries must be imported before core.permissions - a pre-existing kernel
# import-order fragility (core.registries.tool_registry imports
# core.permissions.PermissionDeniedError at module load time, so importing
# core.permissions first triggers a circular partial-init). Documented, not fixed:
# core/ is frozen and this ordering constraint predates this component (identical
# note in agents/founder/orchestrator/agent.py, Phase 4 Component 1).

logger = logging.getLogger("afos.agents.research_pipeline")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

_VALID_DEPTHS = {"standard", "deep"}


class FounderResearchReport(BaseModel):
    """The Research Pipeline's single output contract: one structured report
    covering every stage from Search through Idea Validation.
    """

    idea: str
    venture_id: str
    research_depth: str = "standard"
    search_results: dict[str, Any] = {}
    browser_findings: dict[str, Any] = {}
    market_analysis: dict[str, Any] = {}
    competitor_analysis: dict[str, Any] = {}
    trend_analysis: dict[str, Any] = {}
    opportunity_analysis: dict[str, Any] = {}
    idea_validation: dict[str, Any] = {}
    sources: list[str] = []
    confidence_score: float = 0.0
    stage_results: list[dict[str, Any]] = []
    status: str = "pending"


def run_research_pipeline(idea: str, venture_id: str, research_depth: str = "standard") -> dict:
    """Entry point for running the 7-stage research pipeline. Gated by the existing
    Permission Layer (a self-check against this agent's own granted permissions),
    and confirms every reused worker is actually registered in the existing Agent
    Registry before running (a preflight check, not a bypass - each worker still
    enforces its own permissions/tools independently when it runs). Never bypasses
    the Tool Registry: this pipeline calls no external system directly - it only
    coordinates the already-registered, already-tool-backed research workers.

    Returns a dict shaped {"agent": ..., "event": ..., **FounderResearchReport
    fields}. Never raises for a genuine failure (GraphBubbleUp aside).
    """
    manifest = get_agent_registry().get("research_pipeline")
    get_permission_gate().check(manifest, Permission.INTERNET_ACCESS)

    for stage_agent_name in set(_STAGE_AGENT_NAMES.values()):
        try:
            get_agent_registry().get(stage_agent_name)
        except KeyError:
            logger.warning("research_pipeline: expected worker '%s' is not registered - it will still be invoked, but this is unexpected", stage_agent_name)

    depth = research_depth if research_depth in _VALID_DEPTHS else "standard"

    initial_state: dict[str, Any] = {
        "idea": idea,
        "venture_id": venture_id,
        "phase": "research",
        "research_depth": depth,
    }

    try:
        graph = compile_research_pipeline()
        result = graph.invoke(initial_state)
        report = FounderResearchReport(**result.get("report", {"idea": idea, "venture_id": venture_id, "research_depth": depth}))
        return {"agent": "research_pipeline", "event": "research_pipeline_result", **report.model_dump()}
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("research_pipeline: unexpected pipeline failure for venture '%s': %s", venture_id, exc)
        return {
            "agent": "research_pipeline",
            "event": "research_pipeline_result",
            **FounderResearchReport(idea=idea, venture_id=venture_id, research_depth=depth, status="failed").model_dump(),
        }


def research_pipeline_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses idea/venture_id already
    present on VentureState - no extra state fields required.
    """
    entry = run_research_pipeline(idea=state.get("idea", ""), venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
