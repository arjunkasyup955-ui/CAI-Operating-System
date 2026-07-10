"""Phase 1, Component 4: Market Intelligence Agent.

Verifies:
  1. agent registration + manifest loading - registered with the right supervisor,
     no tools, no permissions (it never touches an external provider).
  2. deterministic analysis using seeded browser data - a fake analyze_fn (injected,
     not the real Model Router) proves the node's plumbing without a live, slow,
     non-deterministic LLM call.
  3. graceful skip when browser output is absent - no exception, a well-formed
     'market_analysis_skipped' entry instead.
  4. graceful failure handling - an analyzer exception is caught and recorded as
     'market_analysis_failed', never raised.
  5. graph execution - Market Intelligence Agent is wired in after Browser Agent and
     runs through the real Manager graph without crashing it.
  6. idempotency - running the full-graph check twice back to back (fresh thread_id
     each time) produces consistent results.
  7. no regression of Components 1-3 (run separately per the regression suite, see
     scripts/smoke_test_phase1_research_supervisor.py,
     scripts/smoke_test_phase1_search_agent.py,
     scripts/smoke_test_phase1_browser_agent.py).

Run: python scripts/smoke_test_phase1_market_intelligence.py
"""

import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from langgraph.types import Command  # noqa: E402

import agents.research.market_intelligence.agent as market_intelligence  # noqa: E402
from core.memory_gateway import get_memory_gateway  # noqa: E402
from core.registries import get_agent_registry  # noqa: E402
from workflows.main_graph import compile_main_graph  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def _fake_analyze(idea: str, content: str, sources: list[str]) -> market_intelligence.MarketIntelligenceReport:
    return market_intelligence.MarketIntelligenceReport(
        market_size_tam="$5B",
        cagr="12%",
        market_segments=["DTC"],
        geography=["US"],
        growth_drivers=["trend A"],
        market_risks=["risk A"],
        major_players=["player A"],
        key_trends=["trend A"],
        sources_used=sources,
        confidence_score=0.8,
    )


def _failing_analyze(idea: str, content: str, sources: list[str]) -> market_intelligence.MarketIntelligenceReport:
    raise RuntimeError("simulated LLM failure")


def main() -> None:
    print("\n== 1. Agent registration + manifest loading ==")
    manifest = get_agent_registry().get("market_intelligence_agent")
    check("registered under research_supervisor", manifest.supervisor == "research_supervisor")
    check("declares no tools (analysis-only, no provider access)", manifest.tools == [])
    check("declares no permissions (makes no Tool Registry calls)", manifest.permissions == [])
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Deterministic analysis using seeded browser data (injected fake analyzer) ==")
    seeded_state = {
        "idea": "A subscription box for artisanal hot sauce",
        "research_findings": [
            {
                "agent": "browser_agent",
                "event": "browser_fetch_completed",
                "url": "https://example.com/hot-sauce-market",
                "content": "The artisanal hot sauce market is growing rapidly across North America.",
            }
        ],
    }
    delta = market_intelligence.run_market_intelligence(seeded_state, _fake_analyze)
    entry = delta["research_findings"][0]
    check("produced market_analysis_completed", entry["event"] == "market_analysis_completed")
    check("market_size_tam matches the seeded deterministic value", entry["market_size_tam"] == "$5B")
    check("cagr matches the seeded deterministic value", entry["cagr"] == "12%")
    check("sources_used reflects the browser_agent URL consumed", entry["sources_used"] == ["https://example.com/hot-sauce-market"])
    check("confidence_score present and well-formed", isinstance(entry["confidence_score"], float))

    print("\n== 3. Graceful skip when browser output is absent ==")
    empty_delta = market_intelligence.run_market_intelligence({"idea": "x", "research_findings": []}, _fake_analyze)
    check("skips instead of raising when no browser content exists", empty_delta["research_findings"][0]["event"] == "market_analysis_skipped")

    print("\n== 4. Graceful failure handling (analyzer raises) ==")
    fail_delta = market_intelligence.run_market_intelligence(seeded_state, _failing_analyze)
    check("catches analyzer exceptions instead of raising", fail_delta["research_findings"][0]["event"] == "market_analysis_failed")

    print("\n== 5 & 6. Full graph execution (twice, for idempotency) ==")
    gateway = get_memory_gateway()
    for i in (1, 2):
        with gateway.working() as checkpointer:
            graph = compile_main_graph(checkpointer)
            venture_id = f"venture-market-intel-test-{uuid.uuid4().hex[:8]}"
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

            mi_entries = [h for h in final["history"] if h.get("agent") == "market_intelligence_agent"]
            check(f"[run {i}] exactly one market_intelligence_agent history entry (no duplication)", len(mi_entries) == 1)
            outcome = mi_entries[0]["event"]
            check(
                f"[run {i}] market_intelligence_agent recorded a well-formed outcome",
                outcome in ("market_analysis_completed", "market_analysis_failed", "market_analysis_skipped"),
            )
            print(f"     [run {i}] outcome in this environment: {outcome}")
            if i == 1:
                first_run_outcome = outcome
            else:
                check("idempotent: both runs reach the same kind of outcome", outcome == first_run_outcome)

        if outcome == "market_analysis_skipped":
            print("     (expected here - no SearXNG/Tavily/Brave configured, so no browser content ever reaches this agent)")

    print("\nAll Phase 1 Component 4 checks passed.")


if __name__ == "__main__":
    main()
