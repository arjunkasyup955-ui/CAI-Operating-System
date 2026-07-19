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

logger = logging.getLogger("afos.agents.market_intelligence")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")


class MarketIntelligenceReport(BaseModel):
    market_size_tam: str = ""
    cagr: str = ""
    market_segments: list[str] = []
    geography: list[str] = []
    growth_drivers: list[str] = []
    market_risks: list[str] = []
    major_players: list[str] = []
    key_trends: list[str] = []
    sources_used: list[str] = []
    confidence_score: float = 0.0


AnalyzeFn = Callable[[str, str, list[str]], MarketIntelligenceReport]


def _gather_browser_content(state: VentureState) -> tuple[str, list[str]]:
    """Reads only what Search/Browser agents already wrote to VentureState - never
    calls a search or browser provider, and never re-fetches anything itself.
    """
    content_parts: list[str] = []
    sources: list[str] = []
    for finding in state.get("research_findings", []):
        if finding.get("agent") == "browser_agent" and finding.get("event") == "browser_fetch_completed":
            content = finding.get("content", "")
            if content:
                content_parts.append(content)
                url = finding.get("url")
                if url:
                    sources.append(url)
    return "\n\n".join(content_parts), sources


def _extract_json(text: str) -> str:
    """LLMs (especially 'thinking' models like qwen3:8b) may wrap the answer in
    reasoning text or markdown fences - pull out the first {...} block rather than
    assuming the whole response is a clean JSON document.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in model response")
    return match.group(0)


def _build_report(idea: str, content: str, sources: list[str]) -> MarketIntelligenceReport:
    """Default analyzer - uses the Model Router (a kernel capability, not a provider
    and not a new tool) to synthesize a structured report. Never called directly by
    market_intelligence_node's tests - see run_market_intelligence's analyze_fn param,
    which injects a deterministic fake for that purpose.
    """
    from core.model_router import get_model_router

    prompt = (
        "You are a market analyst. Based ONLY on the research content below, produce a "
        "JSON object with exactly these keys: market_size_tam (string), cagr (string), "
        "market_segments (list of strings), geography (list of strings), growth_drivers "
        "(list of strings), market_risks (list of strings), major_players (list of "
        "strings), key_trends (list of strings), confidence_score (float 0.0-1.0). "
        "Respond with ONLY the JSON object, no other text.\n\n"
        f"Business idea: {idea}\n\nResearch content:\n{content[:4000]}"
    )
    response = get_model_router().chat(
        [{"role": "user", "content": prompt}],
        capability="high-reasoning",
        agent_name="market_intelligence_agent",
        timeout=300.0,
        json_mode=True,
    )
    data = json.loads(_extract_json(response.content))
    data["sources_used"] = sources
    return MarketIntelligenceReport(**data)


def run_market_intelligence(state: VentureState, analyze_fn: AnalyzeFn = _build_report) -> dict:
    """Core logic, factored out from market_intelligence_node so tests can inject a
    deterministic analyze_fn instead of exercising a live, slow, non-deterministic LLM
    call - the same dependency-injection pattern used for VectorStore's embed_fn and
    Search/BrowserRouter's provider maps.
    """
    content, sources = _gather_browser_content(state)
    bus = get_event_bus()

    if not content:
        entry: dict[str, Any] = {
            "agent": "market_intelligence_agent",
            "event": "market_analysis_skipped",
            "reason": "no usable browser content available",
        }
        bus.publish(
            AFOSEvent(type="market_intelligence.analysis_skipped", source_agent="market_intelligence_agent", payload=entry)
        )
        return {"research_findings": [entry], "history": [entry]}

    try:
        report = analyze_fn(state.get("idea", ""), content, sources)
        entry = {"agent": "market_intelligence_agent", "event": "market_analysis_completed", **report.model_dump()}
        bus.publish(
            AFOSEvent(
                type="market_intelligence.analysis_completed",
                source_agent="market_intelligence_agent",
                payload={"confidence_score": report.confidence_score, "sources_used": report.sources_used},
            )
        )
    except Exception as exc:
        logger.warning("market_intelligence_agent: analysis failed: %s", exc)
        entry = {"agent": "market_intelligence_agent", "event": "market_analysis_failed", "error": str(exc)}
        bus.publish(
            AFOSEvent(type="market_intelligence.analysis_failed", source_agent="market_intelligence_agent", payload=entry)
        )

    return {"research_findings": [entry], "history": [entry]}


def market_intelligence_node(state: VentureState) -> dict:
    """Third real worker inside the Research Supervisor. Consumes only what Search
    Agent and Browser Agent already wrote to state - never accesses a search or
    browser provider, and never calls Playwright/Crawl4AI/Firecrawl/Tavily/Brave/
    SearXNG directly. A failed or skipped analysis is recorded in state, never raised.
    """
    return run_market_intelligence(state, _build_report)
