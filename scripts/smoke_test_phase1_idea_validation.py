"""Phase 1, Component 8: Idea Validation Agent.

Verifies:
  - Agent Registration
  - Manifest
  - Structured Report
  - Dependency Injection
  - Skip Behaviour
  - Failure Handling
  - Event Publishing
  - Workflow Integration

Run: python scripts/smoke_test_phase1_idea_validation.py
"""

import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from langgraph.types import Command  # noqa: E402

import agents.research.idea_validation.agent as idea_validation  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.memory_gateway import get_memory_gateway  # noqa: E402
from core.registries import get_agent_registry  # noqa: E402
from workflows.main_graph import compile_main_graph  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def fake_validation_analyzer(idea: str, context: str, sources: list[str]) -> idea_validation.IdeaValidationReport:
    return idea_validation.IdeaValidationReport(
        overall_score=0.78,
        market_score=0.8,
        competition_score=0.6,
        timing_score=0.85,
        execution_score=0.7,
        differentiation_score=0.75,
        revenue_potential="moderate-to-high",
        risks=["high customer acquisition cost"],
        strengths=["clear niche", "growing market"],
        weaknesses=["low switching cost for customers"],
        recommendation="proceed to MVP planning",
        confidence_score=0.72,
        sources_used=sources,
    )


def failing_validation_analyzer(idea: str, context: str, sources: list[str]) -> idea_validation.IdeaValidationReport:
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
        {
            "agent": "opportunity_detection_agent",
            "event": "opportunity_detection_completed",
            "opportunity_name": "Curated Hot Sauce Discovery Club",
            "problem": "Spice enthusiasts struggle to discover small-batch artisanal hot sauces",
            "target_users": ["spice enthusiasts"],
            "market_gap": "No curated discovery-focused subscription exists",
            "why_now": "Rising DTC food subscription adoption",
            "difficulty": "medium",
            "priority_score": 0.85,
            "sources_used": ["https://example.com/hot-sauce-market"],
        },
    ],
}


def main() -> None:
    print("\n== PASS Agent Registration & Manifest ==")
    manifest = get_agent_registry().get("idea_validation_agent")
    check("registered under research_supervisor", manifest.supervisor == "research_supervisor")
    check("declares no tools (analysis-only, no provider access)", manifest.tools == [])
    check("declares no permissions (makes no Tool Registry calls)", manifest.permissions == [])
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== PASS Structured Report + Dependency Injection ==")
    delta = idea_validation.run_idea_validation(_SEEDED_STATE, analyzer=fake_validation_analyzer)
    entry = delta["research_findings"][0]
    check("produced idea_validation_completed", entry["event"] == "idea_validation_completed")
    for field in (
        "overall_score", "market_score", "competition_score", "timing_score",
        "execution_score", "differentiation_score", "revenue_potential", "risks",
        "strengths", "weaknesses", "recommendation", "confidence_score", "sources_used",
    ):
        check(f"report contains '{field}'", field in entry)
    check("overall_score matches the injected deterministic value", entry["overall_score"] == 0.78)
    check("sources_used reflects upstream findings consumed", entry["sources_used"] == ["https://example.com/hot-sauce-market"])
    check("overall_score is a float", isinstance(entry["overall_score"], float))
    check("confidence_score is a float", isinstance(entry["confidence_score"], float))

    print("\n== PASS Skip Behaviour ==")
    empty_delta = idea_validation.run_idea_validation(
        {"idea": "x", "research_findings": []}, analyzer=fake_validation_analyzer
    )
    check("skips instead of raising with no upstream findings", empty_delta["research_findings"][0]["event"] == "idea_validation_skipped")

    print("\n== PASS Failure Handling ==")
    fail_delta = idea_validation.run_idea_validation(_SEEDED_STATE, analyzer=failing_validation_analyzer)
    check("catches analyzer exceptions instead of raising", fail_delta["research_findings"][0]["event"] == "idea_validation_failed")

    print("\n== PASS Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    idea_validation.run_idea_validation(_SEEDED_STATE, analyzer=fake_validation_analyzer)
    check("published idea_validation_started", "idea_validation_started" in seen_events)
    check("published idea_validation_completed", "idea_validation_completed" in seen_events)

    seen_events.clear()
    idea_validation.run_idea_validation(_SEEDED_STATE, analyzer=failing_validation_analyzer)
    check("published idea_validation_failed", "idea_validation_failed" in seen_events)

    print("\n== PASS Workflow Integration (full graph, twice for idempotency) ==")
    gateway = get_memory_gateway()
    outcomes = []
    for i in (1, 2):
        with gateway.working() as checkpointer:
            graph = compile_main_graph(checkpointer)
            venture_id = f"venture-idea-validation-test-{uuid.uuid4().hex[:8]}"
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

            iv_entries = [h for h in final["history"] if h.get("agent") == "idea_validation_agent"]
            check(f"[run {i}] exactly one idea_validation_agent history entry (no duplication)", len(iv_entries) == 1)
            outcome = iv_entries[0]["event"]
            check(
                f"[run {i}] idea_validation_agent recorded a well-formed outcome",
                outcome in ("idea_validation_completed", "idea_validation_failed", "idea_validation_skipped"),
            )
            print(f"     [run {i}] outcome in this environment: {outcome}")
            outcomes.append(outcome)

    check("idempotent: both runs reach the same kind of outcome", outcomes[0] == outcomes[1])
    if outcomes[0] == "idea_validation_skipped":
        print("     (expected here - no SearXNG/Tavily/Brave configured, so no upstream content ever reaches this agent)")

    print("\nAll Phase 1 Component 8 checks passed.")


if __name__ == "__main__":
    main()
