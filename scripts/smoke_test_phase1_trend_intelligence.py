"""Phase 1, Component 6: Trend Intelligence Agent.

Verifies:
  - Agent Registration
  - Manifest
  - Structured Report
  - Dependency Injection
  - Skip Behaviour
  - Failure Handling
  - Event Publishing
  - Workflow Integration
  - Regression (all previous smoke tests - run separately, see main() note)

Run: python scripts/smoke_test_phase1_trend_intelligence.py
"""

import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from langgraph.types import Command  # noqa: E402

import agents.research.trend_intelligence.agent as trend_intelligence  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.memory_gateway import get_memory_gateway  # noqa: E402
from core.registries import get_agent_registry  # noqa: E402
from workflows.main_graph import compile_main_graph  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def fake_trend_analyzer(idea: str, context: str, sources: list[str]) -> trend_intelligence.TrendIntelligenceReport:
    return trend_intelligence.TrendIntelligenceReport(
        emerging_trends=["AI-personalized products"],
        declining_trends=["generic mass-market boxes"],
        technology_trends=["AI recommendation engines"],
        product_trends=["limited edition drops"],
        pricing_trends=["tiered subscriptions"],
        customer_behavior_trends=["preference for local sourcing"],
        geographic_trends=["growth in southern US"],
        market_signals=["increased VC funding in DTC food"],
        trend_score=0.82,
        trend_summary="Strong upward momentum in artisanal DTC subscriptions.",
        sources_used=sources,
        confidence_score=0.7,
    )


def failing_trend_analyzer(idea: str, context: str, sources: list[str]) -> trend_intelligence.TrendIntelligenceReport:
    raise RuntimeError("simulated LLM failure")


_SEEDED_STATE = {
    "idea": "A subscription box for artisanal hot sauce",
    "research_findings": [
        {
            "agent": "browser_agent",
            "event": "browser_fetch_completed",
            "url": "https://example.com/hot-sauce-market",
            "content": "The artisanal hot sauce market has several established players.",
        },
        {
            "agent": "market_intelligence_agent",
            "event": "market_analysis_completed",
            "market_size_tam": "$5B",
            "cagr": "12%",
            "market_segments": ["DTC"],
            "major_players": ["Player One"],
            "key_trends": ["premiumization"],
            "sources_used": ["https://example.com/hot-sauce-market"],
        },
        {
            "agent": "competitor_intelligence_agent",
            "event": "competitor_analysis_completed",
            "top_competitors": ["CompA"],
            "positioning": "premium",
            "market_gaps": ["underserved SMB segment"],
            "sources_used": ["https://example.com/hot-sauce-market"],
        },
    ],
}


def main() -> None:
    print("\n== PASS Agent Registration & Manifest ==")
    manifest = get_agent_registry().get("trend_intelligence_agent")
    check("registered under research_supervisor", manifest.supervisor == "research_supervisor")
    check("declares no tools (analysis-only, no provider access)", manifest.tools == [])
    check("declares no permissions (makes no Tool Registry calls)", manifest.permissions == [])
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== PASS Structured Report + Dependency Injection ==")
    delta = trend_intelligence.run_trend_intelligence(_SEEDED_STATE, analyzer=fake_trend_analyzer)
    entry = delta["research_findings"][0]
    check("produced trend_analysis_completed", entry["event"] == "trend_analysis_completed")
    for field in (
        "emerging_trends", "declining_trends", "technology_trends", "product_trends",
        "pricing_trends", "customer_behavior_trends", "geographic_trends", "market_signals",
        "trend_score", "trend_summary", "sources_used", "confidence_score",
    ):
        check(f"report contains '{field}'", field in entry)
    check("emerging_trends matches the injected deterministic value", entry["emerging_trends"] == ["AI-personalized products"])
    check("sources_used reflects upstream findings consumed", entry["sources_used"] == ["https://example.com/hot-sauce-market"])
    check("trend_score is a float", isinstance(entry["trend_score"], float))
    check("confidence_score is a float", isinstance(entry["confidence_score"], float))

    print("\n== PASS Skip Behaviour ==")
    empty_delta = trend_intelligence.run_trend_intelligence(
        {"idea": "x", "research_findings": []}, analyzer=fake_trend_analyzer
    )
    check("skips instead of raising with no upstream findings", empty_delta["research_findings"][0]["event"] == "trend_analysis_skipped")

    print("\n== PASS Failure Handling ==")
    fail_delta = trend_intelligence.run_trend_intelligence(_SEEDED_STATE, analyzer=failing_trend_analyzer)
    check("catches analyzer exceptions instead of raising", fail_delta["research_findings"][0]["event"] == "trend_analysis_failed")

    print("\n== PASS Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    trend_intelligence.run_trend_intelligence(_SEEDED_STATE, analyzer=fake_trend_analyzer)
    check("published trend_analysis_started", "trend_analysis_started" in seen_events)
    check("published trend_analysis_completed", "trend_analysis_completed" in seen_events)

    seen_events.clear()
    trend_intelligence.run_trend_intelligence(_SEEDED_STATE, analyzer=failing_trend_analyzer)
    check("published trend_analysis_failed", "trend_analysis_failed" in seen_events)

    print("\n== PASS Workflow Integration (full graph, twice for idempotency) ==")
    gateway = get_memory_gateway()
    outcomes = []
    for i in (1, 2):
        with gateway.working() as checkpointer:
            graph = compile_main_graph(checkpointer)
            venture_id = f"venture-trend-intel-test-{uuid.uuid4().hex[:8]}"
            config = {"configurable": {"thread_id": venture_id}}
            initial_state = {
                "venture_id": venture_id,
                "idea": "A subscription box for artisanal hot sauce",
                "phase": "idea",
            }

            first = graph.invoke(initial_state, config=config)
            check(f"[run {i}] paused on approval interrupt", "__interrupt__" in first)

            final = graph.invoke(Command(resume={"approved": True, "reason": "approved for research"}), config=config)
            check(f"[run {i}] graph completed without crashing", "__interrupt__" not in final)

            ti_entries = [h for h in final["history"] if h.get("agent") == "trend_intelligence_agent"]
            check(f"[run {i}] exactly one trend_intelligence_agent history entry (no duplication)", len(ti_entries) == 1)
            outcome = ti_entries[0]["event"]
            check(
                f"[run {i}] trend_intelligence_agent recorded a well-formed outcome",
                outcome in ("trend_analysis_completed", "trend_analysis_failed", "trend_analysis_skipped"),
            )
            print(f"     [run {i}] outcome in this environment: {outcome}")
            outcomes.append(outcome)

    check("idempotent: both runs reach the same kind of outcome", outcomes[0] == outcomes[1])
    if outcomes[0] == "trend_analysis_skipped":
        print("     (expected here - no SearXNG/Tavily/Brave configured, so no upstream content ever reaches this agent)")

    print("\nAll Phase 1 Component 6 checks passed.")


if __name__ == "__main__":
    main()
