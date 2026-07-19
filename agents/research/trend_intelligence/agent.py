import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, field_validator

from core.event_bus import AFOSEvent, get_event_bus
from core.registries import get_agent_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.trend_intelligence")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")


class TrendIntelligenceReport(BaseModel):
    emerging_trends: list[str] = []
    declining_trends: list[str] = []
    technology_trends: list[str] = []
    product_trends: list[str] = []
    pricing_trends: list[str] = []
    customer_behavior_trends: list[str] = []
    geographic_trends: list[str] = []
    market_signals: list[str] = []
    trend_score: float = 0.0
    trend_summary: str = ""
    sources_used: list[str] = []
    confidence_score: float = 0.0

    @field_validator("trend_summary", mode="before")
    @classmethod
    def _join_list_answers(cls, value: object) -> object:
        """Bug #12: see agents/research/competitor_intelligence/agent.py's
        identical validator - Nemotron occasionally answers a prose field with
        a list of sentences instead of one string despite json_mode.
        """
        if isinstance(value, list):
            return " ".join(str(item) for item in value)
        return value


AnalyzerFn = Callable[[str, str, list[str]], TrendIntelligenceReport]


def _gather_context(state: VentureState) -> tuple[str, list[str]]:
    """Reads only what Browser Agent, Market Intelligence Agent, and Competitor
    Intelligence Agent already wrote to VentureState - never calls a search/browser
    provider or an LLM outside the injectable analyzer seam, and never re-fetches
    anything itself.
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
                f"Market Gaps: {finding.get('market_gaps')}"
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


def _build_report(idea: str, context: str, sources: list[str]) -> TrendIntelligenceReport:
    """Default analyzer - uses the Model Router (a kernel capability, not a provider
    and not a new tool) to synthesize a structured report. Never called directly by
    tests - see run_trend_intelligence's analyzer param, which injects a deterministic
    fake for that purpose (no hardcoded LLM calls in the DI path).
    """
    from core.model_router import get_model_router

    prompt = (
        "You are a market trend analyst. Based ONLY on the research context below "
        "(prior web content, market intelligence, and competitor intelligence), produce "
        "a JSON object with exactly these keys: emerging_trends (list of strings), "
        "declining_trends (list of strings), technology_trends (list of strings), "
        "product_trends (list of strings), pricing_trends (list of strings), "
        "customer_behavior_trends (list of strings), geographic_trends (list of "
        "strings), market_signals (list of strings), trend_score (float 0.0-1.0), "
        "trend_summary (string), confidence_score (float 0.0-1.0). Respond with ONLY "
        "the JSON object, no other text.\n\n"
        f"Business idea: {idea}\n\nResearch context:\n{context[:4000]}"
    )
    response = get_model_router().chat(
        [{"role": "user", "content": prompt}],
        capability="high-reasoning",
        agent_name="trend_intelligence_agent",
        timeout=300.0,
        json_mode=True,
    )
    data = json.loads(_extract_json(response.content))
    data["sources_used"] = sources
    return TrendIntelligenceReport(**data)


def run_trend_intelligence(state: VentureState, analyzer: AnalyzerFn = _build_report) -> dict:
    """Core logic, factored out from trend_intelligence_node so tests can inject a
    deterministic analyzer instead of exercising a live, slow, non-deterministic LLM
    call - same dependency-injection pattern as Market/Competitor Intelligence.
    """
    context, sources = _gather_context(state)
    bus = get_event_bus()

    if not context:
        entry: dict[str, Any] = {
            "agent": "trend_intelligence_agent",
            "event": "trend_analysis_skipped",
            "reason": "no market, competitor, or browser content available",
        }
        bus.publish(
            AFOSEvent(type="trend_analysis_skipped", source_agent="trend_intelligence_agent", payload=entry)
        )
        return {"research_findings": [entry], "history": [entry]}

    bus.publish(
        AFOSEvent(
            type="trend_analysis_started",
            source_agent="trend_intelligence_agent",
            payload={"idea": state.get("idea", "")},
        )
    )

    try:
        report = analyzer(state.get("idea", ""), context, sources)
        entry = {"agent": "trend_intelligence_agent", "event": "trend_analysis_completed", **report.model_dump()}
        bus.publish(
            AFOSEvent(
                type="trend_analysis_completed",
                source_agent="trend_intelligence_agent",
                payload={"confidence_score": report.confidence_score, "sources_used": report.sources_used},
            )
        )
    except Exception as exc:
        logger.warning("trend_intelligence_agent: analysis failed: %s", exc)
        entry = {"agent": "trend_intelligence_agent", "event": "trend_analysis_failed", "error": str(exc)}
        bus.publish(
            AFOSEvent(
                type="trend_analysis_failed",
                source_agent="trend_intelligence_agent",
                payload=entry,
            )
        )

    return {"research_findings": [entry], "history": [entry]}


def trend_intelligence_node(state: VentureState) -> dict:
    """Fifth real worker inside the Research Supervisor. Consumes only what Browser
    Agent, Market Intelligence Agent, and Competitor Intelligence Agent already wrote
    to state - never accesses a search or browser provider, and never calls an LLM
    directly outside the injectable analyzer seam. A failed or skipped analysis is
    recorded in state, never raised.
    """
    return run_trend_intelligence(state, _build_report)
