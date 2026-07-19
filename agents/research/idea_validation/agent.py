import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from core.event_bus import AFOSEvent, get_event_bus
from core.registries import get_agent_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.idea_validation")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")


class IdeaValidationReport(BaseModel):
    overall_score: float = 0.0
    market_score: float = 0.0
    competition_score: float = 0.0
    timing_score: float = 0.0
    execution_score: float = 0.0
    differentiation_score: float = 0.0
    revenue_potential: str = ""
    risks: list[str] = []
    strengths: list[str] = []
    weaknesses: list[str] = []
    recommendation: str = ""
    confidence_score: float = 0.0
    sources_used: list[str] = []


AnalyzerFn = Callable[[str, str, list[str]], IdeaValidationReport]


def _gather_context(state: VentureState) -> tuple[str, list[str]]:
    """Reads only what Browser Agent, Market Intelligence Agent, Competitor
    Intelligence Agent, Trend Intelligence Agent, and Opportunity Detection Agent
    already wrote to VentureState - never calls a search/browser provider or an LLM
    outside the injectable analyzer seam, and never performs its own market analysis.
    """
    context_parts: list[str] = []
    sources: list[str] = []

    for finding in state.get("research_findings", []):
        if finding.get("agent") == "browser_agent" and finding.get("event") == "browser_fetch_completed":
            content = finding.get("content", "")
            if content:
                context_parts.append(f"[Browser content from {finding.get('url')}]\n{content}")
                url = finding.get("url")
                if url:
                    sources.append(url)
        elif finding.get("agent") == "market_intelligence_agent" and finding.get("event") == "market_analysis_completed":
            context_parts.append(
                "[Market Intelligence] "
                f"TAM: {finding.get('market_size_tam')}, CAGR: {finding.get('cagr')}, "
                f"Segments: {finding.get('market_segments')}, Major Players: {finding.get('major_players')}, "
                f"Key Trends: {finding.get('key_trends')}"
            )
            sources.extend(finding.get("sources_used", []))
        elif finding.get("agent") == "competitor_intelligence_agent" and finding.get("event") == "competitor_analysis_completed":
            context_parts.append(
                "[Competitor Intelligence] "
                f"Top Competitors: {finding.get('top_competitors')}, Positioning: {finding.get('positioning')}, "
                f"Market Gaps: {finding.get('market_gaps')}, "
                f"Differentiation Opportunities: {finding.get('differentiation_opportunities')}"
            )
            sources.extend(finding.get("sources_used", []))
        elif finding.get("agent") == "trend_intelligence_agent" and finding.get("event") == "trend_analysis_completed":
            context_parts.append(
                "[Trend Intelligence] "
                f"Emerging Trends: {finding.get('emerging_trends')}, Market Signals: {finding.get('market_signals')}, "
                f"Trend Summary: {finding.get('trend_summary')}"
            )
            sources.extend(finding.get("sources_used", []))
        elif finding.get("agent") == "opportunity_detection_agent" and finding.get("event") == "opportunity_detection_completed":
            context_parts.append(
                "[Opportunity Detected] "
                f"Name: {finding.get('opportunity_name')}, Problem: {finding.get('problem')}, "
                f"Target Users: {finding.get('target_users')}, Market Gap: {finding.get('market_gap')}, "
                f"Why Now: {finding.get('why_now')}, Difficulty: {finding.get('difficulty')}, "
                f"Priority Score: {finding.get('priority_score')}"
            )
            sources.extend(finding.get("sources_used", []))

    seen: set[str] = set()
    unique_sources = [s for s in sources if s and not (s in seen or seen.add(s))]
    return "\n\n".join(context_parts), unique_sources


def _extract_json(text: str) -> str:
    """LLMs (especially 'thinking' models like qwen3:8b) may wrap the answer in
    reasoning text or markdown fences - pull out the first {...} block rather than
    assuming the whole response is a clean JSON document.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in model response")
    return match.group(0)


def _build_report(idea: str, context: str, sources: list[str]) -> IdeaValidationReport:
    """Default analyzer - uses the Model Router (a kernel capability, not a provider
    and not a new tool) to synthesize a structured validation verdict. Never called
    directly by tests - see run_idea_validation's analyzer param, which injects a
    deterministic fake for that purpose (no hardcoded LLM calls in the DI path).
    """
    from core.model_router import get_model_router

    prompt = (
        "You are a venture idea validation analyst. Based ONLY on the research context "
        "below (prior web content, market/competitor/trend intelligence, and the "
        "detected opportunity), produce a JSON object with exactly these keys: "
        "overall_score (float 0.0-1.0), market_score (float 0.0-1.0), "
        "competition_score (float 0.0-1.0), timing_score (float 0.0-1.0), "
        "execution_score (float 0.0-1.0), differentiation_score (float 0.0-1.0), "
        "revenue_potential (string), risks (list of strings), strengths (list of "
        "strings), weaknesses (list of strings), recommendation (string), "
        "confidence_score (float 0.0-1.0). Respond with ONLY the JSON object, no other "
        "text.\n\n"
        f"Business idea: {idea}\n\nResearch context:\n{context[:4000]}"
    )
    response = get_model_router().chat(
        [{"role": "user", "content": prompt}],
        capability="high-reasoning",
        agent_name="idea_validation_agent",
        timeout=300.0,
        json_mode=True,
    )
    data = json.loads(_extract_json(response.content))
    data["sources_used"] = sources
    return IdeaValidationReport(**data)


def run_idea_validation(state: VentureState, analyzer: AnalyzerFn = _build_report) -> dict:
    """Core logic, factored out from idea_validation_node so tests can inject a
    deterministic analyzer instead of exercising a live, slow, non-deterministic LLM
    call - same dependency-injection pattern as every prior Research Supervisor
    analysis worker.
    """
    context, sources = _gather_context(state)
    bus = get_event_bus()

    if not context:
        entry: dict[str, Any] = {
            "agent": "idea_validation_agent",
            "event": "idea_validation_skipped",
            "reason": "no market, competitor, trend, opportunity, or browser content available",
        }
        bus.publish(
            AFOSEvent(type="idea_validation_skipped", source_agent="idea_validation_agent", payload=entry)
        )
        return {"research_findings": [entry], "history": [entry]}

    bus.publish(
        AFOSEvent(
            type="idea_validation_started",
            source_agent="idea_validation_agent",
            payload={"idea": state.get("idea", "")},
        )
    )

    try:
        report = analyzer(state.get("idea", ""), context, sources)
        entry = {"agent": "idea_validation_agent", "event": "idea_validation_completed", **report.model_dump()}
        bus.publish(
            AFOSEvent(
                type="idea_validation_completed",
                source_agent="idea_validation_agent",
                payload={
                    "overall_score": report.overall_score,
                    "confidence_score": report.confidence_score,
                    "sources_used": report.sources_used,
                },
            )
        )
    except Exception as exc:
        logger.warning("idea_validation_agent: analysis failed: %s", exc)
        entry = {"agent": "idea_validation_agent", "event": "idea_validation_failed", "error": str(exc)}
        bus.publish(
            AFOSEvent(
                type="idea_validation_failed",
                source_agent="idea_validation_agent",
                payload=entry,
            )
        )

    return {"research_findings": [entry], "history": [entry]}


def idea_validation_node(state: VentureState) -> dict:
    """Seventh and final worker inside the Research Supervisor for Phase 1. Consumes
    only what Browser Agent, Market Intelligence Agent, Competitor Intelligence Agent,
    Trend Intelligence Agent, and Opportunity Detection Agent already wrote to state -
    never accesses a search or browser provider, and never calls an LLM directly
    outside the injectable analyzer seam. A failed or skipped validation is recorded
    in state, never raised.
    """
    return run_idea_validation(state, _build_report)
