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

logger = logging.getLogger("afos.agents.competitor_intelligence")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")


class CompetitorIntelligenceReport(BaseModel):
    top_competitors: list[str] = []
    direct_competitors: list[str] = []
    indirect_competitors: list[str] = []
    competitor_products: list[str] = []
    pricing_summary: str = ""
    strengths: list[str] = []
    weaknesses: list[str] = []
    positioning: str = ""
    market_gaps: list[str] = []
    differentiation_opportunities: list[str] = []
    sources_used: list[str] = []
    confidence_score: float = 0.0

    @field_validator("pricing_summary", "positioning", mode="before")
    @classmethod
    def _join_list_answers(cls, value: object) -> object:
        """Bug #12: Nemotron occasionally answers a prose field with a list of
        sentences instead of one string (e.g. positioning as
        ['VoltDispatch24 markets ... for same-day repairs.']) despite json_mode
        and an explicit "(string)" instruction. Join rather than reject - the
        content is still usable, just shaped wrong.
        """
        if isinstance(value, list):
            return " ".join(str(item) for item in value)
        return value


AnalyzerFn = Callable[[str, str, list[str]], CompetitorIntelligenceReport]


def _gather_context(state: VentureState) -> tuple[str, list[str]]:
    """Reads only what Browser Agent and Market Intelligence Agent already wrote to
    VentureState - never calls a search/browser provider or the Model Router of
    another agent, and never re-fetches anything itself.
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


def _build_report(idea: str, context: str, sources: list[str]) -> CompetitorIntelligenceReport:
    """Default analyzer - uses the Model Router (a kernel capability, not a provider
    and not a new tool) to synthesize a structured report. Never called directly by
    tests - see run_competitor_intelligence's analyzer param, which injects a
    deterministic fake for that purpose (no hardcoded LLM calls in the DI path).
    """
    from core.model_router import get_model_router

    prompt = (
        "You are a competitive intelligence analyst. Based ONLY on the research context "
        "below (prior web content and market intelligence), produce a JSON object with "
        "exactly these keys: top_competitors (list of strings), direct_competitors (list "
        "of strings), indirect_competitors (list of strings), competitor_products (list "
        "of strings), pricing_summary (string), strengths (list of strings), weaknesses "
        "(list of strings), positioning (string), market_gaps (list of strings), "
        "differentiation_opportunities (list of strings), confidence_score (float "
        "0.0-1.0). Respond with ONLY the JSON object, no other text.\n\n"
        f"Business idea: {idea}\n\nResearch context:\n{context[:4000]}"
    )
    response = get_model_router().chat(
        [{"role": "user", "content": prompt}],
        capability="high-reasoning",
        agent_name="competitor_intelligence_agent",
        timeout=300.0,
        json_mode=True,
    )
    data = json.loads(_extract_json(response.content))
    data["sources_used"] = sources
    return CompetitorIntelligenceReport(**data)


def run_competitor_intelligence(state: VentureState, analyzer: AnalyzerFn = _build_report) -> dict:
    """Core logic, factored out from competitor_intelligence_node so tests can inject
    a deterministic analyzer instead of exercising a live, slow, non-deterministic LLM
    call - same dependency-injection pattern as Market Intelligence's analyze_fn.
    """
    context, sources = _gather_context(state)
    bus = get_event_bus()

    if not context:
        entry: dict[str, Any] = {
            "agent": "competitor_intelligence_agent",
            "event": "competitor_analysis_skipped",
            "reason": "no market intelligence or browser content available",
        }
        bus.publish(
            AFOSEvent(type="competitor_analysis_skipped", source_agent="competitor_intelligence_agent", payload=entry)
        )
        return {"research_findings": [entry], "history": [entry]}

    bus.publish(
        AFOSEvent(
            type="competitor_analysis_started",
            source_agent="competitor_intelligence_agent",
            payload={"idea": state.get("idea", "")},
        )
    )

    try:
        report = analyzer(state.get("idea", ""), context, sources)
        entry = {"agent": "competitor_intelligence_agent", "event": "competitor_analysis_completed", **report.model_dump()}
        bus.publish(
            AFOSEvent(
                type="competitor_analysis_completed",
                source_agent="competitor_intelligence_agent",
                payload={"confidence_score": report.confidence_score, "sources_used": report.sources_used},
            )
        )
    except Exception as exc:
        logger.warning("competitor_intelligence_agent: analysis failed: %s", exc)
        entry = {"agent": "competitor_intelligence_agent", "event": "competitor_analysis_failed", "error": str(exc)}
        bus.publish(
            AFOSEvent(
                type="competitor_analysis_failed",
                source_agent="competitor_intelligence_agent",
                payload=entry,
            )
        )

    return {"research_findings": [entry], "history": [entry]}


def competitor_intelligence_node(state: VentureState) -> dict:
    """Fourth real worker inside the Research Supervisor. Consumes only what Browser
    Agent and Market Intelligence Agent already wrote to state - never accesses a
    search or browser provider, and never calls an LLM directly outside the injectable
    analyzer seam. A failed or skipped analysis is recorded in state, never raised.
    """
    return run_competitor_intelligence(state, _build_report)
