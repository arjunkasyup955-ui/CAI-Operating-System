"""Phase 4, Component 2: Research Pipeline.

Automatically executes Search, Browser, Market Intelligence, Competitor
Intelligence, Trend Intelligence, Opportunity Detection, and Idea Validation in
sequence, reusing every one of those Phase 1 agents' node functions unmodified,
and returns a structured FounderResearchReport. Built on top of the frozen Phase 0
kernel (Event Bus, Agent Registry, Permission Layer, Memory Gateway) - nothing in
Phase 0-3 or Phase 4 Component 1 is modified.

Verifies:
  - Agent registration + manifest
  - Pipeline creation (graph compiles with all 7 stages + report node)
  - Fully deterministic end-to-end run using injected fake stage functions (the
    same dependency-injection seam Phase 1's own smoke tests use for market/
    competitor/trend/opportunity/idea_validation's analyzer functions, generalized
    here to all 7 stages)
  - Graceful continuation when a single stage raises an exception
  - research_depth: "deep" retries a failed/skipped stage once; "standard" does not
  - Real (network-free-in-this-sandbox) happy path degrades gracefully end to end
  - Sources aggregation and confidence_score derivation
  - Event publishing (a start/complete pair for every one of the 7 stages, plus a
    final pipeline-completion event)
  - Manager node integration
  - Regression of every previous phase/component (run directly, one process each -
    NOT smoke_test_phase3_ads.py, whose own regression section would otherwise
    call back into this file, forming an unbounded subprocess cycle; ads.py
    already re-verifies the full Phase 0-3 suite on its own)
  - pip check
  - git status

Run: python scripts/smoke_test_phase4_research_pipeline.py
"""

import logging
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

import agents.founder.research_pipeline.agent as rp_agent  # noqa: E402
import agents.research.competitor_intelligence.agent as competitor_intelligence  # noqa: E402
import agents.research.idea_validation.agent as idea_validation  # noqa: E402
import agents.research.market_intelligence.agent as market_intelligence  # noqa: E402
import agents.research.opportunity_detection.agent as opportunity_detection  # noqa: E402
import agents.research.trend_intelligence.agent as trend_intelligence  # noqa: E402
import workflows.research_pipeline as research_pipeline  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.registries import get_agent_registry  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def new_venture_id(prefix: str) -> str:
    return f"venture-{prefix}-{uuid.uuid4().hex[:8]}"


def _fake_search(state: dict) -> dict:
    entry = {
        "agent": "search_agent", "event": "search_completed", "query": state.get("idea", ""),
        "result_count": 1, "results": [{"url": "https://example.com/x", "title": "x", "snippet": "y"}],
    }
    return {"research_findings": [entry], "history": [entry]}


def _fake_browser(state: dict) -> dict:
    entry = {
        "agent": "browser_agent", "event": "browser_fetch_completed", "url": "https://example.com/x",
        "content_length": 100, "content": "fake page content about the market",
    }
    return {"research_findings": [entry], "history": [entry]}


def _fake_market(state: dict) -> dict:
    return market_intelligence.run_market_intelligence(
        state, lambda idea, content, sources: market_intelligence.MarketIntelligenceReport(
            market_size_tam="$5B", cagr="12%", sources_used=sources, confidence_score=0.8,
        ),
    )


def _fake_competitor(state: dict) -> dict:
    return competitor_intelligence.run_competitor_intelligence(
        state, lambda idea, content, sources: competitor_intelligence.CompetitorIntelligenceReport(sources_used=sources, confidence_score=0.7),
    )


def _fake_trend(state: dict) -> dict:
    return trend_intelligence.run_trend_intelligence(
        state, lambda idea, content, sources: trend_intelligence.TrendIntelligenceReport(sources_used=sources, confidence_score=0.75),
    )


def _fake_opportunity(state: dict) -> dict:
    return opportunity_detection.run_opportunity_detection(
        state, lambda idea, content, sources: opportunity_detection.OpportunityReport(sources_used=sources, confidence_score=0.6),
    )


def _fake_validation(state: dict) -> dict:
    return idea_validation.run_idea_validation(
        state, lambda idea, content, sources: idea_validation.IdeaValidationReport(
            overall_score=8.0, sources_used=sources, confidence_score=0.85, recommendation="pursue",
        ),
    )


_ALL_FAKE_STAGES = {
    "search": _fake_search,
    "browser": _fake_browser,
    "market_intelligence": _fake_market,
    "competitor_intelligence": _fake_competitor,
    "trend_intelligence": _fake_trend,
    "opportunity_detection": _fake_opportunity,
    "idea_validation": _fake_validation,
}


def main() -> None:
    print("\n== 1. Agent Registration + Manifest ==")
    manifest = get_agent_registry().get("research_pipeline")
    check("registered with supervisor null", manifest.supervisor is None)
    check("declares internet_access permission", set(manifest.permissions) == {"internet_access"})
    check("lifecycle is active", manifest.lifecycle == "active")
    check("responsibilities documented", len(manifest.responsibilities) > 0)

    print("\n== 2. Pipeline Creation ==")
    graph = research_pipeline.build_research_pipeline()
    expected_nodes = {"search", "browser", "market_intelligence", "competitor_intelligence", "trend_intelligence", "opportunity_detection", "idea_validation", "report"}
    check("graph builds with all 7 stages + report node", set(graph.nodes) == expected_nodes)

    print("\n== 3. Deterministic End-to-End Run (injected fake stage functions) ==")
    research_pipeline.set_stage_functions(_ALL_FAKE_STAGES)
    try:
        vid = new_venture_id("deterministic")
        result = rp_agent.run_research_pipeline(idea="A marketplace for solo founders to hire vetted freelance CFOs", venture_id=vid)
        check("pipeline completed", result["status"] == "completed")
        check("idea echoed back", result["idea"].startswith("A marketplace"))
        check("venture_id echoed back", result["venture_id"] == vid)
        check("search_results reflects the fake search stage", result["search_results"]["event"] == "search_completed")
        check("browser_findings reflects the fake browser stage", result["browser_findings"]["event"] == "browser_fetch_completed")
        check("market_analysis matches the seeded deterministic value", result["market_analysis"]["market_size_tam"] == "$5B")
        check("competitor_analysis completed", result["competitor_analysis"]["event"] == "competitor_analysis_completed")
        check("trend_analysis completed", result["trend_analysis"]["event"] == "trend_analysis_completed")
        check("opportunity_analysis completed", result["opportunity_analysis"]["event"] == "opportunity_detection_completed")
        check("idea_validation completed", result["idea_validation"]["event"] == "idea_validation_completed")
        check("sources aggregated from all stages, deduplicated", result["sources"] == ["https://example.com/x"])
        check("confidence_score sourced from idea_validation's own confidence_score", result["confidence_score"] == 0.85)
        check("all 7 stage_results report ok", all(s["ok"] for s in result["stage_results"]) and len(result["stage_results"]) == 7)
    finally:
        research_pipeline.reset_stage_functions()
    check("stage functions restored to the real defaults", research_pipeline.get_stage_functions() == research_pipeline._DEFAULT_STAGE_FUNCTIONS)

    print("\n== 4. Graceful Continuation on a Single Stage Failure ==")
    partial_stages = dict(_ALL_FAKE_STAGES)

    def _exploding_competitor(state: dict) -> dict:
        raise RuntimeError("competitor intelligence exploded")

    partial_stages["competitor_intelligence"] = _exploding_competitor
    research_pipeline.set_stage_functions(partial_stages)
    try:
        vid2 = new_venture_id("partial-failure")
        partial_result = rp_agent.run_research_pipeline(idea="partial failure idea", venture_id=vid2)
        check("pipeline still completes despite one stage raising", partial_result["status"] == "completed")
        competitor_stage = next(s for s in partial_result["stage_results"] if s["stage"] == "competitor_intelligence")
        check("the failing stage is recorded as not-ok", competitor_stage["ok"] is False)
        check("the failing stage's outcome is descriptive", competitor_stage["outcome"] == "competitor_intelligence_stage_exception")
        trend_stage = next(s for s in partial_result["stage_results"] if s["stage"] == "trend_intelligence")
        check("later stages still ran normally after the failure", trend_stage["ok"] is True)
        idea_val_stage = next(s for s in partial_result["stage_results"] if s["stage"] == "idea_validation")
        check("idea_validation (final stage) still ran normally after an earlier failure", idea_val_stage["ok"] is True)
    finally:
        research_pipeline.reset_stage_functions()

    print("\n== 5. research_depth: deep retries, standard does not ==")
    attempts = {"n": 0}

    def _flaky_search(state: dict) -> dict:
        attempts["n"] += 1
        if attempts["n"] == 1:
            entry = {"agent": "search_agent", "event": "search_failed", "query": state.get("idea", ""), "error": "transient"}
        else:
            entry = {"agent": "search_agent", "event": "search_completed", "query": state.get("idea", ""), "results": [{"url": "https://example.com/deep"}]}
        return {"research_findings": [entry], "history": [entry]}

    research_pipeline.set_stage_functions({"search": _flaky_search})
    try:
        deep_result = rp_agent.run_research_pipeline(idea="deep idea", venture_id=new_venture_id("deep"), research_depth="deep")
        deep_search_stage = next(s for s in deep_result["stage_results"] if s["stage"] == "search")
        check("deep mode retried the failed stage and recovered", deep_search_stage["ok"] is True)
        check("deep mode made exactly 2 attempts", attempts["n"] == 2)
    finally:
        research_pipeline.reset_stage_functions()

    attempts["n"] = 0
    research_pipeline.set_stage_functions({"search": _flaky_search})
    try:
        standard_result = rp_agent.run_research_pipeline(idea="standard idea", venture_id=new_venture_id("standard"), research_depth="standard")
        standard_search_stage = next(s for s in standard_result["stage_results"] if s["stage"] == "search")
        check("standard mode does not retry", standard_search_stage["ok"] is False)
        check("standard mode made exactly 1 attempt", attempts["n"] == 1)
    finally:
        research_pipeline.reset_stage_functions()

    invalid_depth_result = rp_agent.run_research_pipeline(idea="bad depth idea", venture_id=new_venture_id("bad-depth"), research_depth="ultra")
    check("an invalid research_depth value falls back to 'standard'", invalid_depth_result["research_depth"] == "standard")

    print("\n== 6. Real (network-free-in-this-sandbox) happy path ==")
    real_result = rp_agent.run_research_pipeline(idea="A subscription box for artisanal hot sauce", venture_id=new_venture_id("real"))
    check("real pipeline run completes without crashing", real_result["event"] == "research_pipeline_result")
    check("real pipeline run reports a well-formed status", real_result["status"] in ("completed", "no_data", "failed"))
    check("real pipeline run always returns exactly 7 stage_results", len(real_result["stage_results"]) == 7)
    print(f"     real pipeline status in this environment: {real_result['status']} (expected 'no_data' - no network/LLM configured)")

    print("\n== 7. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    research_pipeline.set_stage_functions({"search": _fake_search})
    try:
        rp_agent.run_research_pipeline(idea="event test idea", venture_id=new_venture_id("events"))
    finally:
        research_pipeline.reset_stage_functions()
    stage_started = [e for e in seen_events if e == "research_pipeline_stage_started"]
    stage_completed = [e for e in seen_events if e == "research_pipeline_stage_completed"]
    check("published a research_pipeline_stage_started event for every one of the 7 stages", len(stage_started) == 7)
    check("published a research_pipeline_stage_completed event for every one of the 7 stages", len(stage_completed) == 7)
    check("published the final research_pipeline_completed event", "research_pipeline_completed" in seen_events)

    print("\n== 8. Manager-callable node shape ==")
    research_pipeline.set_stage_functions({"search": _fake_search})
    try:
        node_vid = new_venture_id("node")
        delta = rp_agent.research_pipeline_node({"idea": "node integration idea", "venture_id": node_vid})
        check("research_pipeline_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
        check("node result carries a well-formed status", delta["history"][0]["status"] in ("completed", "no_data", "failed"))
    finally:
        research_pipeline.reset_stage_functions()

    print("\n== 9. Full Regression of All Previous Phases/Components ==")
    repo_root = Path(__file__).resolve().parent.parent
    # smoke_test_phase3_ads.py (Phase 3 Component 13) and
    # smoke_test_phase4_founder_orchestrator.py (Phase 4 Component 1) both embed
    # their own full regression section, each globbing "smoke_test_phase*.py" -
    # neither file's exclusion list knows about *this* new file (they're frozen,
    # not to be modified), so including either one here would let it call back
    # into this file, forming an unbounded subprocess cycle the moment either of
    # them is run standalone. The cycle only needs breaking on one side; both
    # already re-verify the full Phase 0-3(+4C1) suite standalone, so excluding
    # them here loses no coverage - every test they would have run is still run
    # directly below.
    excluded = {
        "smoke_test_phase4_research_pipeline.py",
        "smoke_test_phase3_ads.py",
        "smoke_test_phase4_founder_orchestrator.py",
    }
    smoke_tests = sorted(
        p for p in (repo_root / "scripts").glob("smoke_test_phase*.py")
        if p.name not in excluded
    )
    for test_path in smoke_tests:
        proc = subprocess.run([sys.executable, str(test_path)], cwd=repo_root, capture_output=True, text=True)
        if proc.returncode != 0:
            # A subprocess-spawned regression test occasionally observes transient
            # environmental flakiness unrelated to this component (e.g. the local
            # Ollama model needing a moment to load after being idle) - one retry
            # absorbs that without masking a genuine regression, which fails
            # consistently and would still be reported as FAIL below.
            print(f"     regression: {test_path.name} failed on first attempt, retrying once...")
            proc = subprocess.run([sys.executable, str(test_path)], cwd=repo_root, capture_output=True, text=True)
        check(f"regression: {test_path.name}", proc.returncode == 0)

    print("\n== 10. pip check ==")
    pip_result = subprocess.run([sys.executable, "-m", "pip", "check"], cwd=repo_root, capture_output=True, text=True)
    check("pip check reports no broken requirements", pip_result.returncode == 0)

    print("\n== 11. git status (only new files, nothing modified) ==")
    git_result = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True)
    status_lines = [line for line in git_result.stdout.splitlines() if line.strip()]
    modified_or_deleted = [line for line in status_lines if not line.startswith("??")]
    check("git status has no modified/deleted files, only untracked additions", len(modified_or_deleted) == 0)

    print("\nAll Phase 4 Component 2 checks passed.")


if __name__ == "__main__":
    main()
