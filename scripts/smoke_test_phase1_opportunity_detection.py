"""Phase 1, Component 7: Opportunity Detection Agent.

Verifies:
  - Agent Registration
  - Manifest
  - Structured Report
  - Dependency Injection
  - Skip Behaviour
  - Failure Handling
  - Event Publishing
  - Workflow Integration

Run: python scripts/smoke_test_phase1_opportunity_detection.py
"""

import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from langgraph.types import Command  # noqa: E402

import agents.research.opportunity_detection.agent as opportunity_detection  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.memory_gateway import get_memory_gateway  # noqa: E402
from core.registries import get_agent_registry  # noqa: E402
from workflows.main_graph import compile_main_graph  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def fake_opportunity_analyzer(idea: str, context: str, sources: list[str]) -> opportunity_detection.OpportunityReport:
    return opportunity_detection.OpportunityReport(
        opportunity_name="Curated Hot Sauce Discovery Club",
        opportunity_type="subscription",
        problem="Spice enthusiasts struggle to discover small-batch artisanal hot sauces",
        target_users=["spice enthusiasts", "foodies"],
        market_gap="No curated discovery-focused subscription exists",
        why_now="Rising DTC food subscription adoption",
        competitors_missing=["CompA"],
        estimated_market="$500M SAM",
        monetization_model="monthly subscription",
        difficulty="medium",
        priority_score=0.85,
        confidence_score=0.7,
        sources_used=sources,
    )


def failing_opportunity_analyzer(idea: str, context: str, sources: list[str]) -> opportunity_detection.OpportunityReport:
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
            "differentiation_opportunities": ["faster onboarding"],
            "sources_used": ["https://example.com/hot-sauce-market"],
        },
        {
            "agent": "trend_intelligence_agent",
            "event": "trend_analysis_completed",
            "emerging_trends": ["AI-personalized products"],
            "market_signals": ["increased VC funding in DTC food"],
            "trend_summary": "Strong upward momentum in artisanal DTC subscriptions.",
            "sources_used": ["https://example.com/hot-sauce-market"],
        },
    ],
}


def main() -> None:
    print("\n== PASS Agent Registration & Manifest ==")
    manifest = get_agent_registry().get("opportunity_detection_agent")
    check("registered under research_supervisor", manifest.supervisor == "research_supervisor")
    check("declares no tools (analysis-only, no provider access)", manifest.tools == [])
    check("declares no permissions (makes no Tool Registry calls)", manifest.permissions == [])
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== PASS Structured Report + Dependency Injection ==")
    delta = opportunity_detection.run_opportunity_detection(_SEEDED_STATE, analyzer=fake_opportunity_analyzer)
    entry = delta["research_findings"][0]
    check("produced opportunity_detection_completed", entry["event"] == "opportunity_detection_completed")
    for field in (
        "opportunity_name", "opportunity_type", "problem", "target_users", "market_gap",
        "why_now", "competitors_missing", "estimated_market", "monetization_model",
        "difficulty", "priority_score", "confidence_score", "sources_used",
    ):
        check(f"report contains '{field}'", field in entry)
    check("opportunity_name matches the injected deterministic value", entry["opportunity_name"] == "Curated Hot Sauce Discovery Club")
    check("sources_used reflects upstream findings consumed", entry["sources_used"] == ["https://example.com/hot-sauce-market"])
    check("priority_score is a float", isinstance(entry["priority_score"], float))
    check("confidence_score is a float", isinstance(entry["confidence_score"], float))

    print("\n== PASS Skip Behaviour ==")
    empty_delta = opportunity_detection.run_opportunity_detection(
        {"idea": "x", "research_findings": []}, analyzer=fake_opportunity_analyzer
    )
    check("skips instead of raising with no upstream findings", empty_delta["research_findings"][0]["event"] == "opportunity_detection_skipped")

    print("\n== PASS Failure Handling ==")
    fail_delta = opportunity_detection.run_opportunity_detection(_SEEDED_STATE, analyzer=failing_opportunity_analyzer)
    check("catches analyzer exceptions instead of raising", fail_delta["research_findings"][0]["event"] == "opportunity_detection_failed")

    print("\n== PASS Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    opportunity_detection.run_opportunity_detection(_SEEDED_STATE, analyzer=fake_opportunity_analyzer)
    check("published opportunity_detection_started", "opportunity_detection_started" in seen_events)
    check("published opportunity_detection_completed", "opportunity_detection_completed" in seen_events)

    seen_events.clear()
    opportunity_detection.run_opportunity_detection(_SEEDED_STATE, analyzer=failing_opportunity_analyzer)
    check("published opportunity_detection_failed", "opportunity_detection_failed" in seen_events)

    print("\n== PASS Workflow Integration (full graph, twice for idempotency) ==")
    gateway = get_memory_gateway()
    outcomes = []
    for i in (1, 2):
        with gateway.working() as checkpointer:
            graph = compile_main_graph(checkpointer)
            venture_id = f"venture-opportunity-test-{uuid.uuid4().hex[:8]}"
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

            od_entries = [h for h in final["history"] if h.get("agent") == "opportunity_detection_agent"]
            check(f"[run {i}] exactly one opportunity_detection_agent history entry (no duplication)", len(od_entries) == 1)
            outcome = od_entries[0]["event"]
            check(
                f"[run {i}] opportunity_detection_agent recorded a well-formed outcome",
                outcome in ("opportunity_detection_completed", "opportunity_detection_failed", "opportunity_detection_skipped"),
            )
            print(f"     [run {i}] outcome in this environment: {outcome}")
            outcomes.append(outcome)

    check("idempotent: both runs reach the same kind of outcome", outcomes[0] == outcomes[1])
    if outcomes[0] == "opportunity_detection_skipped":
        print("     (expected here - no SearXNG/Tavily/Brave configured, so no upstream content ever reaches this agent)")

    print("\nAll Phase 1 Component 7 checks passed.")


if __name__ == "__main__":
    main()
