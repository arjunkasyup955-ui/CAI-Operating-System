import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp
from pydantic import BaseModel

from core.registries import get_agent_registry
from core.state import VentureState
from workflows.growth_pipeline import compile_growth_pipeline

logger = logging.getLogger("afos.agents.growth_pipeline")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

_VALID_DEPTHS = {"standard", "deep"}


class LandingPage(BaseModel):
    generated: bool = False
    content: str = ""
    error: str = ""


class SEOMetadata(BaseModel):
    generated: bool = False
    title: str = ""
    description: str = ""
    keywords: list[str] = []
    error: str = ""


class MarketingAssets(BaseModel):
    generated: bool = False
    tagline: str = ""
    error: str = ""


class AdCampaignDraft(BaseModel):
    ready: bool = False
    audiences: list[dict[str, Any]] = []
    keywords: list[dict[str, Any]] = []
    error: str = ""


class CRMConfiguration(BaseModel):
    ready: bool = False
    company_created: bool = False
    pipelines_available: list[str] = []
    error: str = ""


class GrowthReport(BaseModel):
    """The Growth Pipeline's single output contract - a summary of the landing
    page/SEO/marketing/ad-draft/CRM-prep run assembled entirely from the
    already-completed Deployment Pipeline's Deployment Report and the reused
    Phase 3 Ollama/Ads/CRM Agents' own results.
    """

    idea: str
    status: str = "pending"
    deployment_validated: bool = False
    landing_page: LandingPage = LandingPage()
    seo_metadata: SEOMetadata = SEOMetadata()
    marketing_assets: MarketingAssets = MarketingAssets()
    ad_campaign_draft: AdCampaignDraft = AdCampaignDraft()
    crm_configuration: CRMConfiguration = CRMConfiguration()
    ready_stage_count: int = 0
    total_stage_count: int = 0
    summary: str = ""
    error: str = ""


def run_growth_pipeline(idea: str, venture_id: str, research_depth: str = "standard") -> dict:
    """Entry point for running the Growth Pipeline. Registered in the existing
    Agent Registry above; declares no permissions and calls the Tool Registry
    for nothing directly - it only ever reaches outside itself through the
    already-completed Deployment Pipeline (Phase 4 Component 6) and the
    existing, unmodified Phase 3 Ollama/Ads/CRM Agents it orchestrates (see
    workflows/growth_pipeline.py). Never raises for a genuine failure
    (GraphBubbleUp aside) - invalid input, a failed/unsuccessful deployment, and
    a partially/fully unreachable growth backend all degrade to a well-formed
    GrowthReport with a descriptive status.

    Returns a dict shaped {"agent": ..., "event": ..., **GrowthReport fields}.
    """
    depth = research_depth if research_depth in _VALID_DEPTHS else "standard"

    initial_state: dict[str, Any] = {
        "idea": idea,
        "venture_id": venture_id,
        "phase": "marketing",
        "research_depth": depth,
    }

    try:
        graph = compile_growth_pipeline()
        result = graph.invoke(initial_state)
        report = GrowthReport(**result.get("growth_report", {"idea": idea, "status": "failed"}))
        return {"agent": "growth_pipeline", "event": "growth_pipeline_result", **report.model_dump()}
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("growth_pipeline: unexpected pipeline failure for venture '%s': %s", venture_id, exc)
        return {
            "agent": "growth_pipeline",
            "event": "growth_pipeline_result",
            **GrowthReport(idea=idea, status="failed", error=str(exc)).model_dump(),
        }


def growth_pipeline_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses idea/venture_id already
    present on VentureState - no extra state fields required.
    """
    entry = run_growth_pipeline(idea=state.get("idea", ""), venture_id=state.get("venture_id", "default"))
    return {"history": [entry]}
