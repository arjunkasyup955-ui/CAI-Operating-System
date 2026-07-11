"""Phase 4, Component 3: Founder Decision Engine.

Consumes the completed Research Pipeline's (Phase 4 Component 2) FounderResearchReport
and produces a deterministic, pure-Python FounderDecisionReport - no AI/LLM calls,
no external API calls of its own. Built on top of the frozen Phase 0 kernel (Event
Bus, Agent Registry) and the already-verified Phase 4 Components 1-2 - nothing in
Phase 0-3 or Phase 4 Components 1-2 is modified.

Verifies:
  - Agent registration + manifest
  - Graph creation (5 nodes: intake, research, extract_metrics, calculate_scores,
    aggregate)
  - Score calculation (all 9 dimension scores + confidence_score, each 0-10)
  - Recommendation logic (all 4 tiers: BUILD NOW / VALIDATE FIRST / PIVOT / DROP,
    exercised via injected fake Research Pipeline results spanning strong,
    borderline, and weak signals)
  - Invalid input (empty idea, missing venture_id) rejected gracefully
  - Event publishing (decision_started/decision_completed/decision_failed)
  - Deterministic output (same input -> byte-identical scores, run twice)
  - Manager node integration
  - Regression of every previous phase/component (run directly, one process each -
    excluding smoke_test_phase3_ads.py, smoke_test_phase4_founder_orchestrator.py,
    and smoke_test_phase4_research_pipeline.py, each of which embeds its own full
    regression section that would otherwise call back into this file, forming an
    unbounded subprocess cycle - all three already re-verify the full prior suite
    standalone)
  - pip check
  - git status

Run: python scripts/smoke_test_phase4_decision_engine.py
"""

import logging
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

import agents.founder.decision_engine.agent as de_agent  # noqa: E402
import workflows.decision_engine as decision_engine  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.registries import get_agent_registry  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def new_venture_id(prefix: str) -> str:
    return f"venture-{prefix}-{uuid.uuid4().hex[:8]}"


def _all_stages_ok() -> list[dict]:
    return [
        {"stage": s, "ok": True}
        for s in ("search", "browser", "market_intelligence", "competitor_intelligence", "trend_intelligence", "opportunity_detection", "idea_validation")
    ]


_STRONG_REPORT = {
    "idea": "An AI copilot for solo founders", "status": "completed",
    "market_analysis": {"market_size_tam": "$12B", "cagr": "25%", "growth_drivers": ["a", "b", "c"], "market_risks": [], "confidence_score": 0.9},
    "competitor_analysis": {"direct_competitors": [], "market_gaps": ["gap1", "gap2"], "differentiation_opportunities": ["d1"], "confidence_score": 0.85},
    "trend_analysis": {"emerging_trends": ["t1", "t2", "t3"], "declining_trends": [], "trend_score": 0.9, "confidence_score": 0.8},
    "opportunity_analysis": {"priority_score": 0.9, "difficulty": "low", "monetization_model": "subscription", "confidence_score": 0.85},
    "idea_validation": {
        "event": "idea_validation_completed", "overall_score": 9.0, "competition_score": 9.0, "timing_score": 9.0,
        "execution_score": 8.0, "revenue_potential": "very high", "risks": [], "confidence_score": 0.9,
    },
    "sources": ["s1", "s2"], "confidence_score": 0.85, "stage_results": _all_stages_ok(),
}

_BORDERLINE_REPORT = {
    "idea": "A niche B2B SaaS analytics tool", "status": "completed",
    "market_analysis": {"market_size_tam": "$300M", "cagr": "8%", "growth_drivers": ["a"], "market_risks": ["r1"], "confidence_score": 0.6},
    "competitor_analysis": {"direct_competitors": ["c1", "c2"], "market_gaps": ["g1"], "differentiation_opportunities": [], "confidence_score": 0.5},
    "trend_analysis": {"emerging_trends": ["t1"], "declining_trends": ["d1"], "trend_score": 0.5, "confidence_score": 0.5},
    "opportunity_analysis": {"priority_score": 0.5, "difficulty": "medium", "monetization_model": "subscription", "confidence_score": 0.5},
    "idea_validation": {"event": "idea_validation_skipped"},
    "sources": ["s1"], "confidence_score": 0.5, "stage_results": _all_stages_ok(),
}

_WEAK_REPORT = {
    "idea": "A generic todo app", "status": "completed",
    "market_analysis": {"market_size_tam": "$2M", "cagr": "1%", "growth_drivers": [], "market_risks": ["r1", "r2", "r3"], "confidence_score": 0.4},
    "competitor_analysis": {"direct_competitors": ["c1", "c2", "c3", "c4", "c5"], "market_gaps": [], "differentiation_opportunities": [], "confidence_score": 0.3},
    "trend_analysis": {"emerging_trends": [], "declining_trends": ["d1", "d2"], "trend_score": 0.1, "confidence_score": 0.3},
    "opportunity_analysis": {"priority_score": 0.1, "difficulty": "high", "monetization_model": "", "confidence_score": 0.2},
    "idea_validation": {"event": "idea_validation_skipped"},
    "sources": [], "confidence_score": 0.2,
    "stage_results": [{"stage": s, "ok": s != "idea_validation"} for s in ("search", "browser", "market_intelligence", "competitor_intelligence", "trend_intelligence", "opportunity_detection", "idea_validation")],
}


def main() -> None:
    print("\n== 1. Agent Registration + Manifest ==")
    manifest = get_agent_registry().get("decision_engine")
    check("registered with supervisor null", manifest.supervisor is None)
    check("declares no permissions (no new permission types, no Tool Registry calls)", manifest.permissions == [])
    check("declares no tools", manifest.tools == [])
    check("lifecycle is active", manifest.lifecycle == "active")
    check("responsibilities documented", len(manifest.responsibilities) > 0)

    print("\n== 2. Graph Creation ==")
    graph = decision_engine.build_decision_engine()
    expected_nodes = {"intake", "research", "extract_metrics", "calculate_scores", "aggregate"}
    check("graph builds with all 5 nodes", set(graph.nodes) == expected_nodes)

    print("\n== 3. Score Calculation (strong signal report) ==")
    decision_engine.set_research_pipeline_invoker(lambda idea, vid, depth: _STRONG_REPORT)
    try:
        strong = de_agent.run_decision_engine(idea="An AI copilot for solo founders", venture_id=new_venture_id("strong"))
        check("pipeline completed", strong["status"] == "completed")
        for field in (
            "opportunity_score", "competition_score", "risk_score", "market_size_score", "build_difficulty",
            "execution_complexity", "revenue_potential", "market_timing", "ai_advantage", "confidence_score", "overall_score",
        ):
            check(f"{field} is a float in [0, 10]", isinstance(strong[field], float) and 0.0 <= strong[field] <= 10.0)
        check("strong market size ($12B) yields a high market_size_score", strong["market_size_score"] >= 7.0)
        check("zero direct competitors yields a high competition_score", strong["competition_score"] >= 7.0)
        check("AI-related idea text yields a high ai_advantage", strong["ai_advantage"] > 5.0)
        check("idea_validation's own overall_score contributes to a high confidence_score", strong["confidence_score"] >= 5.0)
        check("reasons list is non-empty for a strong report", len(strong["reasons"]) > 0)
        check("no warnings for a fully-strong report", strong["warnings"] == [])
    finally:
        decision_engine.reset_research_pipeline_invoker()

    print("\n== 4. Recommendation Logic (all 4 tiers) ==")
    decision_engine.set_research_pipeline_invoker(lambda idea, vid, depth: _STRONG_REPORT)
    try:
        r = de_agent.run_decision_engine(idea="strong idea", venture_id=new_venture_id("tier-build"))
        check("strong signals -> BUILD NOW", r["recommendation"] == "BUILD NOW")
        check("BUILD NOW overall_score >= 8", r["overall_score"] >= 8.0)
        check("BUILD NOW next_actions mention MVP development", any("MVP" in a for a in r["next_actions"]))
    finally:
        decision_engine.reset_research_pipeline_invoker()

    decision_engine.set_research_pipeline_invoker(lambda idea, vid, depth: _BORDERLINE_REPORT)
    try:
        r2 = de_agent.run_decision_engine(idea="borderline idea", venture_id=new_venture_id("tier-mid"))
        check("borderline signals land in VALIDATE FIRST or PIVOT", r2["recommendation"] in ("VALIDATE FIRST", "PIVOT"))
        check("4 <= overall_score < 8 for a borderline report", 4.0 <= r2["overall_score"] < 8.0)
    finally:
        decision_engine.reset_research_pipeline_invoker()

    decision_engine.set_research_pipeline_invoker(lambda idea, vid, depth: _WEAK_REPORT)
    try:
        r3 = de_agent.run_decision_engine(idea="weak idea", venture_id=new_venture_id("tier-drop"))
        check("weak signals -> DROP", r3["recommendation"] == "DROP")
        check("DROP overall_score < 4", r3["overall_score"] < 4.0)
        check("DROP produces at least one warning", len(r3["warnings"]) > 0)
        check("DROP next_actions advise against further resources", any("Do not allocate" in a for a in r3["next_actions"]))
    finally:
        decision_engine.reset_research_pipeline_invoker()

    print("\n== 5. Invalid Input ==")
    empty_idea = de_agent.run_decision_engine(idea="", venture_id=new_venture_id("empty-idea"))
    check("empty idea is rejected gracefully", empty_idea["status"] == "rejected")
    check("rejected report still has all required fields well-formed", empty_idea["recommendation"] == "DROP" and empty_idea["overall_score"] == 0.0)
    check("rejected report carries a descriptive warning", "idea" in " ".join(empty_idea["warnings"]).lower())

    missing_venture = de_agent.run_decision_engine(idea="some idea", venture_id="")
    check("missing venture_id is rejected gracefully", missing_venture["status"] == "rejected")

    invalid_depth = de_agent.run_decision_engine(idea="some idea", venture_id=new_venture_id("bad-depth"), research_depth="ultra")
    check("an invalid research_depth is silently coerced to 'standard', not rejected", invalid_depth["status"] in ("completed",))

    print("\n== 6. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    decision_engine.set_research_pipeline_invoker(lambda idea, vid, depth: _STRONG_REPORT)
    try:
        de_agent.run_decision_engine(idea="event test idea", venture_id=new_venture_id("events"))
    finally:
        decision_engine.reset_research_pipeline_invoker()
    check("published decision_started", "decision_started" in seen_events)
    check("published decision_completed", "decision_completed" in seen_events)
    check("did not publish decision_failed for a successful run", "decision_failed" not in seen_events)

    seen_events.clear()
    de_agent.run_decision_engine(idea="", venture_id=new_venture_id("events-rejected"))
    check("published decision_started even for a rejected run", "decision_started" in seen_events)
    check("published decision_failed for a rejected run", "decision_failed" in seen_events)
    check("did not publish decision_completed for a rejected run", "decision_completed" not in seen_events)

    print("\n== 7. Deterministic Output ==")
    decision_engine.set_research_pipeline_invoker(lambda idea, vid, depth: _STRONG_REPORT)
    try:
        vid = new_venture_id("deterministic")
        first_run = de_agent.run_decision_engine(idea="deterministic idea", venture_id=vid)
        second_run = de_agent.run_decision_engine(idea="deterministic idea", venture_id=vid)
        for field in (
            "opportunity_score", "competition_score", "risk_score", "market_size_score", "build_difficulty",
            "execution_complexity", "revenue_potential", "market_timing", "ai_advantage", "confidence_score",
            "overall_score", "recommendation",
        ):
            check(f"{field} identical across two runs of the same input", first_run[field] == second_run[field])
    finally:
        decision_engine.reset_research_pipeline_invoker()
    check("research pipeline invoker restored to the real default", decision_engine.get_research_pipeline_invoker() is decision_engine._default_research_pipeline_invoker)

    print("\n== 8. Manager-callable node shape ==")
    decision_engine.set_research_pipeline_invoker(lambda idea, vid, depth: _STRONG_REPORT)
    try:
        delta = de_agent.decision_engine_node({"idea": "node integration idea", "venture_id": new_venture_id("node")})
        check("decision_engine_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
        check("node result carries a completed status", delta["history"][0]["status"] == "completed")
    finally:
        decision_engine.reset_research_pipeline_invoker()

    print("\n== 9. Full Regression of All Previous Phases/Components ==")
    repo_root = Path(__file__).resolve().parent.parent
    # These three files each embed their own full regression section, globbing
    # "smoke_test_phase*.py" - none of their (frozen, not to be modified)
    # exclusion lists know about this new file, so including any of them here
    # would let it call back into this file, forming an unbounded subprocess
    # cycle. All three already re-verify the full prior suite standalone, so
    # excluding them here loses no coverage.
    excluded = {
        "smoke_test_phase4_decision_engine.py",
        "smoke_test_phase3_ads.py",
        "smoke_test_phase4_founder_orchestrator.py",
        "smoke_test_phase4_research_pipeline.py",
    }
    smoke_tests = sorted(
        p for p in (repo_root / "scripts").glob("smoke_test_phase*.py")
        if p.name not in excluded
    )
    for test_path in smoke_tests:
        proc = subprocess.run([sys.executable, str(test_path)], cwd=repo_root, capture_output=True, text=True)
        if proc.returncode != 0:
            # One retry absorbs transient environmental flakiness (e.g. the local
            # Ollama model needing a moment to load after being idle) without
            # masking a genuine regression, which fails consistently.
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

    print("\nAll Phase 4 Component 3 checks passed.")


if __name__ == "__main__":
    main()
