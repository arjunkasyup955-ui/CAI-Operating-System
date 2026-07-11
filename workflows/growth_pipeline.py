import re
from collections.abc import Callable
from typing import Any

from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents.founder.deployment_pipeline.agent import run_deployment_pipeline
from agents.infrastructure.ads.agent import run_ads_operation
from agents.infrastructure.crm.agent import run_crm_operation
from agents.llm.ollama.agent import run_ollama_operation
from core.event_bus import AFOSEvent, get_event_bus
from core.state import VentureState

# --------------------------------------------------------------------------- #
# Dependency injection: the Deployment Pipeline is the *only* thing this module
# calls to get a Deployment Report - never a Tool Registry tool, an LLM, or a
# raw HTTP call of its own. Swappable, not cached, so tests can inject a
# deterministic fake DeploymentReport-shaped dict instead of exercising the
# real research + decision + planning + build + deployment chain. Same
# convention as every prior Phase 4 component's get_x_invoker()/set_x_invoker()
# pair.
# --------------------------------------------------------------------------- #

DeploymentPipelineInvoker = Callable[[str, str, str], dict[str, Any]]


def _default_deployment_pipeline_invoker(idea: str, venture_id: str, research_depth: str) -> dict[str, Any]:
    return run_deployment_pipeline(idea, venture_id, research_depth)


_deployment_pipeline_invoker: DeploymentPipelineInvoker = _default_deployment_pipeline_invoker


def get_deployment_pipeline_invoker() -> DeploymentPipelineInvoker:
    return _deployment_pipeline_invoker


def set_deployment_pipeline_invoker(fn: DeploymentPipelineInvoker) -> None:
    global _deployment_pipeline_invoker
    _deployment_pipeline_invoker = fn


def reset_deployment_pipeline_invoker() -> None:
    global _deployment_pipeline_invoker
    _deployment_pipeline_invoker = _default_deployment_pipeline_invoker


# --------------------------------------------------------------------------- #
# Dependency injection for each of the 3 reused Phase 3 agents this pipeline
# calls (Ollama - infrastructure, used for content generation; Ads and CRM -
# marketing-related). Lets tests inject a deterministic fake run_x_operation-
# shaped callable per agent (any subset) instead of exercising the real,
# network/LLM/service-dependent backends. Same convention as Component 6's
# get_infra_operations()/set_infra_operations() pair.
# --------------------------------------------------------------------------- #

GrowthOperationFn = Callable[..., dict[str, Any]]

_DEFAULT_GROWTH_OPERATIONS: dict[str, GrowthOperationFn] = {
    "ollama": run_ollama_operation,
    "ads": run_ads_operation,
    "crm": run_crm_operation,
}

_growth_operations: dict[str, GrowthOperationFn] = dict(_DEFAULT_GROWTH_OPERATIONS)


def get_growth_operations() -> dict[str, GrowthOperationFn]:
    return _growth_operations


def set_growth_operations(overrides: dict[str, GrowthOperationFn]) -> None:
    """Swappable, not cached - lets tests inject deterministic fake operation
    dispatchers for any subset of the 3 reused agents. Unspecified agents keep
    their default (real) dispatcher.
    """
    global _growth_operations
    _growth_operations = {**_DEFAULT_GROWTH_OPERATIONS, **overrides}


def reset_growth_operations() -> None:
    global _growth_operations
    _growth_operations = dict(_DEFAULT_GROWTH_OPERATIONS)


class GrowthPipelineState(VentureState, total=False):
    """Extends VentureState - per its own docstring ("Extend this, never create a
    parallel state shape") - with the fields this workflow needs.
    """

    research_depth: str
    status: str
    error: str
    deployment_report: dict[str, Any]
    landing_page: dict[str, Any]
    seo_metadata: dict[str, Any]
    marketing_assets: dict[str, Any]
    ad_campaign_draft: dict[str, Any]
    crm_configuration: dict[str, Any]
    growth_report: dict[str, Any]


def _ok(entry: dict[str, Any]) -> bool:
    return str(entry.get("event", "")).endswith("_completed")


def _call(agent_key: str, operation: str, venture_id: str, **kwargs: Any) -> dict[str, Any]:
    fn = get_growth_operations()[agent_key]
    try:
        return fn(operation, venture_id=venture_id, **kwargs)
    except GraphBubbleUp:
        raise
    except Exception as exc:
        return {"event": f"{agent_key}_failed", "error": str(exc)}


_STOPWORDS = {
    "a", "an", "the", "for", "and", "or", "to", "of", "in", "on", "with", "that",
    "this", "is", "are", "be", "by", "from", "as", "at", "it", "its",
}


def _extract_keywords(idea: str, limit: int = 8) -> list[str]:
    """Pure Python, deterministic keyword extraction - no AI/LLM call. The same
    idea text always produces the identical keyword list.
    """
    words = re.findall(r"[A-Za-z][A-Za-z0-9'-]*", idea.lower())
    seen: list[str] = []
    for word in words:
        if word in _STOPWORDS or len(word) < 3:
            continue
        if word not in seen:
            seen.append(word)
    return seen[:limit]


def _validate_inputs(state: GrowthPipelineState) -> str | None:
    """research_depth is not validated here: run_growth_pipeline() (agents/
    founder/growth_pipeline/agent.py) already coerces any value outside
    {"standard", "deep"} to "standard" before this graph ever runs - the same
    silent-coercion convention used by every prior Phase 4 component for the
    same field.
    """
    idea = (state.get("idea") or "").strip()
    if not idea:
        return "idea must not be empty"
    venture_id = (state.get("venture_id") or "").strip()
    if not venture_id:
        return "venture_id is required"
    return None


def intake_node(state: GrowthPipelineState) -> dict:
    bus = get_event_bus()
    venture_id = state.get("venture_id", "")
    idea = state.get("idea", "")

    bus.publish(AFOSEvent(type="growth_started", source_agent="growth_pipeline", venture_id=venture_id, payload={"idea": idea}))

    error = _validate_inputs(state)
    if error:
        return {"status": "rejected", "error": error}
    return {"status": "validated"}


def _route_after_intake(state: GrowthPipelineState) -> str:
    return "deployment_report" if state.get("status") == "validated" else "aggregate"


def deployment_report_node(state: GrowthPipelineState) -> dict:
    """The only place this workflow ever reaches outside itself for a deployment
    to validate - only through the already-completed Deployment Pipeline's own
    public function, never a Tool Registry tool, LLM, or raw external call
    directly. This *is* requirement #1 ("Validate deployment success").
    """
    try:
        report = get_deployment_pipeline_invoker()(
            state.get("idea", ""), state.get("venture_id", ""), state.get("research_depth") or "standard",
        )
        return {"deployment_report": report, "status": "deployment_received"}
    except GraphBubbleUp:
        raise
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


def _route_after_deployment_report(state: GrowthPipelineState) -> str:
    if state.get("status") != "deployment_received":
        return "aggregate"
    if state.get("deployment_report", {}).get("status") != "completed":
        return "aggregate"
    return "landing_page"


def landing_page_node(state: GrowthPipelineState) -> dict:
    """Requirement #2 ("Generate Landing Page") - delegates copywriting to the
    Ollama Agent (Phase 3 Infrastructure), never calls the Model Router or an
    LLM provider directly.
    """
    venture_id = state.get("venture_id", "")
    idea = state.get("idea", "")
    result = _call(
        "ollama", "ollama_chat", venture_id,
        messages=[{"role": "user", "content": f"Write concise landing page hero copy (one headline, one two-sentence subheadline) for this startup idea: {idea}. Respond with plain text only."}],
    )
    ok = _ok(result)
    return {"landing_page": {"generated": ok, "content": result.get("content", "") if ok else "", "error": "" if ok else str(result.get("error", "landing page generation failed"))}}


def seo_metadata_node(state: GrowthPipelineState) -> dict:
    """Requirement #3 ("Generate SEO metadata") - pure Python, deterministic
    templating from the idea and the landing page copy already generated above.
    No AI/LLM call, no external API call.
    """
    idea = state.get("idea", "")
    landing_page = state.get("landing_page", {})
    keywords = _extract_keywords(idea)
    description_source = landing_page.get("content") or idea
    description = description_source.strip().replace("\n", " ")[:155]
    return {
        "seo_metadata": {
            "generated": True,
            "title": f"{idea} | Official Site"[:60],
            "description": description,
            "keywords": keywords,
            "error": "",
        }
    }


def marketing_assets_node(state: GrowthPipelineState) -> dict:
    """Requirement #4 ("Generate Marketing Assets") - delegates copywriting to
    the Ollama Agent, same reasoning as the landing page step.
    """
    venture_id = state.get("venture_id", "")
    idea = state.get("idea", "")
    result = _call(
        "ollama", "ollama_chat", venture_id,
        messages=[{"role": "user", "content": f"Write one short marketing tagline (under 12 words) for this startup idea: {idea}. Respond with plain text only, no quotes."}],
    )
    ok = _ok(result)
    return {"marketing_assets": {"generated": ok, "tagline": result.get("content", "") if ok else "", "error": "" if ok else str(result.get("error", "marketing asset generation failed"))}}


def ad_campaign_draft_node(state: GrowthPipelineState) -> dict:
    """Requirement #5 ("Generate Ad Campaign Draft") - uses the Ads Agent's
    read-only audience/keyword research operations (both low risk) to prepare a
    non-committal draft; never calls ads_create_campaign, so no live ad
    campaign is ever actually created by a "draft" step.
    """
    venture_id = state.get("venture_id", "")
    idea = state.get("idea", "")
    audience_result = _call("ads", "ads_audience_lookup", venture_id, query=idea)
    keyword_result = _call("ads", "ads_keyword_suggestions", venture_id, seed_keyword=idea)

    audience_ok = _ok(audience_result)
    keyword_ok = _ok(keyword_result)
    ready = audience_ok and keyword_ok
    error_parts = [
        str(audience_result.get("error", "")) if not audience_ok else "",
        str(keyword_result.get("error", "")) if not keyword_ok else "",
    ]
    return {
        "ad_campaign_draft": {
            "ready": ready,
            "audiences": audience_result.get("audiences", []) if audience_ok else [],
            "keywords": keyword_result.get("keywords", []) if keyword_ok else [],
            "error": "; ".join(p for p in error_parts if p),
        }
    }


def crm_configuration_node(state: GrowthPipelineState) -> dict:
    """Requirement #6 ("Prepare CRM configuration") - uses the CRM Agent to
    confirm connectivity, register the venture as a company record, and confirm
    a pipeline is available to receive its leads.
    """
    venture_id = state.get("venture_id", "")
    idea = state.get("idea", "")

    health_result = _call("crm", "crm_health_check", venture_id)
    if not _ok(health_result):
        return {"crm_configuration": {"ready": False, "company_created": False, "pipelines_available": [], "error": str(health_result.get("error", "CRM is unreachable"))}}

    company_result = _call("crm", "crm_create_company", venture_id, company_name=idea[:255])
    pipelines_result = _call("crm", "crm_list_pipelines", venture_id)

    company_ok = _ok(company_result)
    pipelines_ok = _ok(pipelines_result)
    ready = company_ok and pipelines_ok
    error_parts = [
        str(company_result.get("error", "")) if not company_ok else "",
        str(pipelines_result.get("error", "")) if not pipelines_ok else "",
    ]
    return {
        "crm_configuration": {
            "ready": ready,
            "company_created": company_ok,
            "pipelines_available": [p.get("pipeline_name", "") for p in pipelines_result.get("pipelines", [])] if pipelines_ok else [],
            "error": "; ".join(p for p in error_parts if p),
        }
    }


def aggregate_node(state: GrowthPipelineState) -> dict:
    """Requirement #7 ("Produce Growth Report")."""
    bus = get_event_bus()
    venture_id = state.get("venture_id", "")
    idea = state.get("idea", "")
    status = state.get("status", "failed")

    if status in ("rejected", "failed"):
        reason = state.get("error", "unexpected failure")
        report = {
            "idea": idea, "status": status, "deployment_validated": False,
            "landing_page": {}, "seo_metadata": {}, "marketing_assets": {}, "ad_campaign_draft": {}, "crm_configuration": {},
            "ready_stage_count": 0, "total_stage_count": 0, "summary": reason, "error": reason,
        }
        bus.publish(AFOSEvent(type="growth_failed", source_agent="growth_pipeline", venture_id=venture_id, payload={"reason": reason, "status": status}))
        return {"growth_report": report, "history": [{"agent": "growth_pipeline", "event": "growth_failed", "status": status}]}

    deployment_report = state.get("deployment_report", {})
    if deployment_report.get("status") != "completed":
        reason = f"Deployment was not successful (status='{deployment_report.get('status', 'unknown')}') - growth activities skipped"
        report = {
            "idea": idea, "status": "skipped", "deployment_validated": False,
            "landing_page": {}, "seo_metadata": {}, "marketing_assets": {}, "ad_campaign_draft": {}, "crm_configuration": {},
            "ready_stage_count": 0, "total_stage_count": 0, "summary": reason, "error": reason,
        }
        bus.publish(AFOSEvent(type="growth_failed", source_agent="growth_pipeline", venture_id=venture_id, payload={"reason": reason, "status": "skipped"}))
        return {"growth_report": report, "history": [{"agent": "growth_pipeline", "event": "growth_failed", "status": "skipped"}]}

    landing_page = state.get("landing_page", {"generated": False})
    seo_metadata = state.get("seo_metadata", {"generated": False})
    marketing_assets = state.get("marketing_assets", {"generated": False})
    ad_campaign_draft = state.get("ad_campaign_draft", {"ready": False})
    crm_configuration = state.get("crm_configuration", {"ready": False})

    readiness = [
        bool(landing_page.get("generated")),
        bool(seo_metadata.get("generated")),
        bool(marketing_assets.get("generated")),
        bool(ad_campaign_draft.get("ready")),
        bool(crm_configuration.get("ready")),
    ]
    ready_count = sum(readiness)
    total = len(readiness)
    final_status = "completed" if ready_count == total else ("partial" if ready_count > 0 else "failed")

    errors = [
        landing_page.get("error", ""), marketing_assets.get("error", ""),
        ad_campaign_draft.get("error", ""), crm_configuration.get("error", ""),
    ]
    report = {
        "idea": idea,
        "status": final_status,
        "deployment_validated": True,
        "landing_page": landing_page,
        "seo_metadata": seo_metadata,
        "marketing_assets": marketing_assets,
        "ad_campaign_draft": ad_campaign_draft,
        "crm_configuration": crm_configuration,
        "ready_stage_count": ready_count,
        "total_stage_count": total,
        "summary": f"Growth pipeline for '{idea}': {ready_count}/{total} growth activities ready.",
        "error": "; ".join(e for e in errors if e),
    }

    if final_status == "completed":
        bus.publish(AFOSEvent(type="growth_completed", source_agent="growth_pipeline", venture_id=venture_id, payload={"ready_stage_count": ready_count, "total_stage_count": total}))
        return {"growth_report": report, "history": [{"agent": "growth_pipeline", "event": "growth_completed", "status": final_status}]}

    bus.publish(AFOSEvent(type="growth_failed", source_agent="growth_pipeline", venture_id=venture_id, payload={"reason": report["error"], "status": final_status}))
    return {"growth_report": report, "history": [{"agent": "growth_pipeline", "event": "growth_failed", "status": final_status}]}


def build_growth_pipeline() -> StateGraph:
    """Deployment Report -> Validate -> Landing Page -> SEO Metadata -> Marketing
    Assets -> Ad Campaign Draft -> CRM Configuration -> Growth Report. Invalid
    input, a failed Deployment Report fetch, or a Deployment Report that isn't
    successful (status != "completed") all short-circuit straight to
    "aggregate" with a well-formed, explanatory Growth Report - never attempting
    any growth activity. Each growth stage always runs regardless of a prior
    stage's outcome - one unreachable backend (e.g. Ollama or the CRM/Ads
    backends being unconfigured in this sandbox) degrades only that stage's
    result, never aborts the pipeline, matching Component 6's Deployment
    Pipeline's own "gracefully continue if one stage fails" precedent.
    """
    graph = StateGraph(GrowthPipelineState)
    graph.add_node("intake", intake_node)
    graph.add_node("deployment_report", deployment_report_node)
    graph.add_node("landing_page", landing_page_node)
    graph.add_node("seo_metadata", seo_metadata_node)
    graph.add_node("marketing_assets", marketing_assets_node)
    graph.add_node("ad_campaign_draft", ad_campaign_draft_node)
    graph.add_node("crm_configuration", crm_configuration_node)
    graph.add_node("aggregate", aggregate_node)

    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", _route_after_intake, ["deployment_report", "aggregate"])
    graph.add_conditional_edges("deployment_report", _route_after_deployment_report, ["landing_page", "aggregate"])
    graph.add_edge("landing_page", "seo_metadata")
    graph.add_edge("seo_metadata", "marketing_assets")
    graph.add_edge("marketing_assets", "ad_campaign_draft")
    graph.add_edge("ad_campaign_draft", "crm_configuration")
    graph.add_edge("crm_configuration", "aggregate")
    graph.add_edge("aggregate", END)
    return graph


def compile_growth_pipeline() -> CompiledStateGraph:
    # Deliberately compiled without a checkpointer, same precedent as
    # workflows/deployment_pipeline.py's compile_deployment_pipeline() - this
    # workflow runs straight through with no approval gate of its own (the
    # reused Ollama/Ads/CRM Agents own their own approval gates internally,
    # unchanged, and every operation used here is low/medium risk so none of
    # them pause).
    return build_growth_pipeline().compile()
