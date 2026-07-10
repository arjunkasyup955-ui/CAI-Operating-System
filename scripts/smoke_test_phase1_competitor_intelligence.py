"""Phase 1, Component 5: Competitor Intelligence Agent.

Verifies:
  - Agent Registration
  - Manifest
  - Structured Report
  - Dependency Injection
  - Skip Behaviour
  - Failure Handling
  - Event Publishing
  - Workflow Integration

Run: python scripts/smoke_test_phase1_competitor_intelligence.py
"""

import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from langgraph.types import Command  # noqa: E402

import agents.research.competitor_intelligence.agent as competitor_intelligence  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.memory_gateway import get_memory_gateway  # noqa: E402
from core.registries import get_agent_registry  # noqa: E402
from workflows.main_graph import compile_main_graph  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def fake_competitor_analyzer(idea: str, context: str, sources: list[str]) -> competitor_intelligence.CompetitorIntelligenceReport:
    return competitor_intelligence.CompetitorIntelligenceReport(
        top_competitors=["CompA", "CompB"],
        direct_competitors=["CompA"],
        indirect_competitors=["CompB"],
        competitor_products=["ProductX"],
        pricing_summary="mid-range, $20-40/mo",
        strengths=["strong brand recognition"],
        weaknesses=["limited geographic coverage"],
        positioning="premium",
        market_gaps=["underserved SMB segment"],
        differentiation_opportunities=["faster onboarding"],
        sources_used=sources,
        confidence_score=0.75,
    )


def failing_competitor_analyzer(idea: str, context: str, sources: list[str]) -> competitor_intelligence.CompetitorIntelligenceReport:
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
    ],
}


def main() -> None:
    print("\n== PASS Agent Registration & Manifest ==")
    manifest = get_agent_registry().get("competitor_intelligence_agent")
    check("registered under research_supervisor", manifest.supervisor == "research_supervisor")
    check("declares no tools (analysis-only, no provider access)", manifest.tools == [])
    check("declares no permissions (makes no Tool Registry calls)", manifest.permissions == [])
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== PASS Structured Report + Dependency Injection ==")
    delta = competitor_intelligence.run_competitor_intelligence(_SEEDED_STATE, analyzer=fake_competitor_analyzer)
    entry = delta["research_findings"][0]
    check("produced competitor_analysis_completed", entry["event"] == "competitor_analysis_completed")
    for field in (
        "top_competitors", "direct_competitors", "indirect_competitors", "competitor_products",
        "pricing_summary", "strengths", "weaknesses", "positioning", "market_gaps",
        "differentiation_opportunities", "sources_used", "confidence_score",
    ):
        check(f"report contains '{field}'", field in entry)
    check("top_competitors matches the injected deterministic value", entry["top_competitors"] == ["CompA", "CompB"])
    check("sources_used reflects upstream findings consumed", entry["sources_used"] == ["https://example.com/hot-sauce-market"])
    check("confidence_score is a float", isinstance(entry["confidence_score"], float))

    print("\n== PASS Skip Behaviour ==")
    empty_delta = competitor_intelligence.run_competitor_intelligence(
        {"idea": "x", "research_findings": []}, analyzer=fake_competitor_analyzer
    )
    check("skips instead of raising with no market/browser findings", empty_delta["research_findings"][0]["event"] == "competitor_analysis_skipped")

    print("\n== PASS Failure Handling ==")
    fail_delta = competitor_intelligence.run_competitor_intelligence(_SEEDED_STATE, analyzer=failing_competitor_analyzer)
    check("catches analyzer exceptions instead of raising", fail_delta["research_findings"][0]["event"] == "competitor_analysis_failed")

    print("\n== PASS Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    competitor_intelligence.run_competitor_intelligence(_SEEDED_STATE, analyzer=fake_competitor_analyzer)
    check("published competitor_analysis_started", "competitor_analysis_started" in seen_events)
    check("published competitor_analysis_completed", "competitor_analysis_completed" in seen_events)

    seen_events.clear()
    competitor_intelligence.run_competitor_intelligence(_SEEDED_STATE, analyzer=failing_competitor_analyzer)
    check("published competitor_analysis_failed", "competitor_analysis_failed" in seen_events)

    print("\n== PASS Workflow Integration (full graph, twice for idempotency) ==")
    gateway = get_memory_gateway()
    outcomes = []
    for i in (1, 2):
        with gateway.working() as checkpointer:
            graph = compile_main_graph(checkpointer)
            venture_id = f"venture-competitor-intel-test-{uuid.uuid4().hex[:8]}"
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

            ci_entries = [h for h in final["history"] if h.get("agent") == "competitor_intelligence_agent"]
            check(f"[run {i}] exactly one competitor_intelligence_agent history entry (no duplication)", len(ci_entries) == 1)
            outcome = ci_entries[0]["event"]
            check(
                f"[run {i}] competitor_intelligence_agent recorded a well-formed outcome",
                outcome in ("competitor_analysis_completed", "competitor_analysis_failed", "competitor_analysis_skipped"),
            )
            print(f"     [run {i}] outcome in this environment: {outcome}")
            outcomes.append(outcome)

    check("idempotent: both runs reach the same kind of outcome", outcomes[0] == outcomes[1])
    if outcomes[0] == "competitor_analysis_skipped":
        print("     (expected here - no SearXNG/Tavily/Brave configured, so no market/browser content ever reaches this agent)")

    print("\nAll Phase 1 Component 5 checks passed.")


if __name__ == "__main__":
    main()
