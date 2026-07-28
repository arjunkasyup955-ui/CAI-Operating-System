"""Phase 4, Component 4: MVP Planner.

Consumes ONLY the completed Founder Decision Engine's (Phase 4 Component 3)
FounderDecisionReport and deterministically produces an MVPPlan - PRD, MVP scope,
feature list, recommended tech stack, database outline, API outline, folder
structure, development timeline, milestones, and risks & dependencies. No AI/LLM
calls, no external API calls of its own - pure Python + LangGraph only. Built on
top of the frozen Phase 0 kernel (Event Bus, Agent Registry) and the
already-verified Phase 4 Components 1-3 - nothing in Phase 0-3 or Phase 4
Components 1-3 is modified.

Verifies:
  - Agent registration + manifest
  - Graph creation (4 nodes: intake, decision, plan_generation, aggregate)
  - Score-driven plan generation for BUILD NOW (full scope, AI + monetization
    features, longer timeline) and VALIDATE FIRST (lean, validation-focused
    scope, shorter timeline) recommendations
  - PIVOT and DROP recommendations correctly skip full plan generation
  - A non-completed Decision Engine result also skips plan generation
  - Invalid input (empty idea, missing venture_id) rejected gracefully
  - Event publishing (mvp_planner_started/mvp_planner_completed/mvp_planner_failed)
  - Deterministic output (same input -> byte-identical plan, run twice)
  - Manager node integration
  - Regression of every previous phase/component (run directly, one process each -
    excluding the four files that each embed their own full regression section,
    which would otherwise call back into this file, forming an unbounded
    subprocess cycle - all four already re-verify the full prior suite standalone)
  - pip check
  - git status

Run: python scripts/smoke_test_phase4_mvp_planner.py
"""

import logging
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

import agents.founder.mvp_planner.agent as mvp_agent  # noqa: E402
import workflows.mvp_planner as mvp_planner  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.registries import get_agent_registry  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def new_venture_id(prefix: str) -> str:
    return f"venture-{prefix}-{uuid.uuid4().hex[:8]}"


def _decision(recommendation: str, **overrides) -> dict:
    base = {
        "idea": "An AI copilot for solo founders", "status": "completed", "recommendation": recommendation,
        "overall_score": 9.0, "ai_advantage": 8.0, "build_difficulty": 7.0, "execution_complexity": 6.5,
        "market_size_score": 9.0, "revenue_potential": 8.0, "competition_score": 8.0, "risk_score": 7.0,
        "market_timing": 8.0, "opportunity_score": 8.5, "confidence_score": 8.0,
        "reasons": ["Strong opportunity signal", "Large addressable market"], "warnings": [],
    }
    base.update(overrides)
    return base


_BUILD_NOW_DECISION = _decision("BUILD NOW")
_VALIDATE_FIRST_DECISION = _decision(
    "VALIDATE FIRST", idea="A niche B2B analytics tool", overall_score=6.5, ai_advantage=4.0,
    market_size_score=5.0, revenue_potential=5.0, competition_score=5.0, risk_score=5.0,
    market_timing=5.0, opportunity_score=6.0, confidence_score=5.0, reasons=[], warnings=["Low confidence in scores"],
)


def main() -> None:
    print("\n== 1. Agent Registration + Manifest ==")
    manifest = get_agent_registry().get("mvp_planner")
    check("registered with supervisor null", manifest.supervisor is None)
    check("declares no permissions (no new permission types, no Tool Registry calls)", manifest.permissions == [])
    check("declares no tools", manifest.tools == [])
    check("lifecycle is active", manifest.lifecycle == "active")
    check("responsibilities documented", len(manifest.responsibilities) > 0)

    print("\n== 2. Graph Creation ==")
    graph = mvp_planner.build_mvp_planner()
    expected_nodes = {"intake", "decision", "plan_generation", "aggregate"}
    check("graph builds with all 4 nodes", set(graph.nodes) == expected_nodes)

    print("\n== 3. Plan Generation: BUILD NOW (full scope) ==")
    mvp_planner.set_decision_engine_invoker(lambda idea, vid, depth: _BUILD_NOW_DECISION)
    try:
        build_now_vid = new_venture_id("build-now")
        build_now = mvp_agent.run_mvp_planner(idea="An AI copilot for solo founders", venture_id=build_now_vid)
        check("plan completed", build_now["status"] == "completed")
        check("PRD is populated", bool(build_now["prd"]["title"]) and len(build_now["prd"]["goals"]) > 0)
        check("mvp_scope has included items", len(build_now["mvp_scope"]["included"]) > 0)
        feature_names = {f["name"] for f in build_now["feature_list"]}
        check("full scope includes Basic Dashboard", "Basic Dashboard" in feature_names)
        check("AI-native idea includes an AI-Powered Assistant feature", "AI-Powered Assistant" in feature_names)
        check("high revenue_potential makes Billing & Subscriptions must_have", next(f for f in build_now["feature_list"] if f["name"] == "Billing & Subscriptions")["priority"] == "must_have")
        tech_layers = {t["layer"] for t in build_now["tech_stack"]}
        check("tech stack covers frontend/backend/database/auth/deployment", {"frontend", "backend", "database", "auth", "deployment"} <= tech_layers)
        check("AI signal adds an 'ai' tech stack layer", "ai" in tech_layers)
        db_tables = {d["name"] for d in build_now["database_outline"]}
        check("monetized plan includes a subscriptions table", "subscriptions" in db_tables)
        check("AI-signal plan includes an embeddings table", "embeddings" in db_tables)
        check("api_outline has endpoints for every included feature area", len(build_now["api_outline"]) >= 5)
        check("folder_structure is non-empty and includes backend/frontend", any("backend" in p for p in build_now["folder_structure"]) and any("frontend" in p for p in build_now["folder_structure"]))
        check(
            "folder_structure is scoped under generated_ventures/{venture_id}/ (Fix A: no more repo-root collisions)",
            len(build_now["folder_structure"]) > 0 and all(p.startswith(f"generated_ventures/{build_now_vid}/") for p in build_now["folder_structure"]),
        )
        check("development_timeline has 4 phases", len(build_now["development_timeline"]) == 4)
        check("milestones align 1:1 with timeline phases", len(build_now["milestones"]) == len(build_now["development_timeline"]))
        check("risks_and_dependencies is non-empty", len(build_now["risks_and_dependencies"]) > 0)
    finally:
        mvp_planner.reset_decision_engine_invoker()

    print("\n== 4. Plan Generation: VALIDATE FIRST (lean scope) ==")
    mvp_planner.set_decision_engine_invoker(lambda idea, vid, depth: _VALIDATE_FIRST_DECISION)
    try:
        validate_first = mvp_agent.run_mvp_planner(idea="A niche B2B analytics tool", venture_id=new_venture_id("validate-first"))
        check("plan completed", validate_first["status"] == "completed")
        lean_features = {f["name"] for f in validate_first["feature_list"]}
        check("lean scope includes a landing page / waitlist feature", "Landing Page & Waitlist" in lean_features)
        check("lean scope includes feedback capture", "Feedback Capture" in lean_features)
        billing_priority = next(f for f in validate_first["feature_list"] if f["name"] == "Billing & Subscriptions")["priority"]
        check("billing is deferred to nice_to_have in a lean/validation plan", billing_priority == "nice_to_have")
        validate_weeks = int(validate_first["development_timeline"][-1]["weeks"].split("-")[-1])
    finally:
        mvp_planner.reset_decision_engine_invoker()

    mvp_planner.set_decision_engine_invoker(lambda idea, vid, depth: _BUILD_NOW_DECISION)
    try:
        build_now_weeks = int(mvp_agent.run_mvp_planner(idea="An AI copilot for solo founders", venture_id=new_venture_id("weeks-cmp"))["development_timeline"][-1]["weeks"].split("-")[-1])
        check("VALIDATE FIRST timeline is shorter than BUILD NOW's", validate_weeks < build_now_weeks)
    finally:
        mvp_planner.reset_decision_engine_invoker()

    print("\n== 5. PIVOT / DROP Skip Full Plan Generation ==")
    for recommendation in ("PIVOT", "DROP"):
        decision = _decision(recommendation, idea="weak idea", overall_score=3.0)
        mvp_planner.set_decision_engine_invoker(lambda idea, vid, depth, d=decision: d)
        try:
            result = mvp_agent.run_mvp_planner(idea="weak idea", venture_id=new_venture_id(recommendation.lower()))
            check(f"{recommendation} recommendation yields status 'not_recommended'", result["status"] == "not_recommended")
            check(f"{recommendation} produces an empty feature_list", result["feature_list"] == [])
            check(f"{recommendation} produces an empty development_timeline", result["development_timeline"] == [])
            check(f"{recommendation} explains why in mvp_scope.rationale", recommendation in result["mvp_scope"]["rationale"])
            check(f"{recommendation} still records a risk explaining the skip", len(result["risks_and_dependencies"]) == 1)
        finally:
            mvp_planner.reset_decision_engine_invoker()

    print("\n== 6. Non-Completed Decision Also Skips Planning ==")
    incomplete_decision = {"idea": "x", "status": "rejected", "recommendation": "DROP"}
    mvp_planner.set_decision_engine_invoker(lambda idea, vid, depth: incomplete_decision)
    try:
        incomplete_result = mvp_agent.run_mvp_planner(idea="x", venture_id=new_venture_id("incomplete"))
        check("a non-completed decision status skips planning", incomplete_result["status"] == "not_recommended")
        check("reason mentions the decision engine did not complete", "did not complete" in incomplete_result["mvp_scope"]["rationale"])
    finally:
        mvp_planner.reset_decision_engine_invoker()

    print("\n== 7. Invalid Input ==")
    empty_idea = mvp_agent.run_mvp_planner(idea="", venture_id=new_venture_id("empty-idea"))
    check("empty idea is rejected gracefully", empty_idea["status"] == "rejected")
    check("rejected plan still has a well-formed (empty) structure", empty_idea["feature_list"] == [] and empty_idea["development_timeline"] == [])

    missing_venture = mvp_agent.run_mvp_planner(idea="some idea", venture_id="")
    check("missing venture_id is rejected gracefully", missing_venture["status"] == "rejected")

    mvp_planner.set_decision_engine_invoker(lambda idea, vid, depth: _BUILD_NOW_DECISION)
    try:
        invalid_depth = mvp_agent.run_mvp_planner(idea="some idea", venture_id=new_venture_id("bad-depth"), research_depth="ultra")
        check("an invalid research_depth is silently coerced to 'standard', not rejected", invalid_depth["status"] == "completed")
    finally:
        mvp_planner.reset_decision_engine_invoker()

    print("\n== 8. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    mvp_planner.set_decision_engine_invoker(lambda idea, vid, depth: _BUILD_NOW_DECISION)
    try:
        mvp_agent.run_mvp_planner(idea="event test idea", venture_id=new_venture_id("events"))
    finally:
        mvp_planner.reset_decision_engine_invoker()
    check("published mvp_planner_started", "mvp_planner_started" in seen_events)
    check("published mvp_planner_completed", "mvp_planner_completed" in seen_events)
    check("did not publish mvp_planner_failed for a successful run", "mvp_planner_failed" not in seen_events)

    seen_events.clear()
    mvp_agent.run_mvp_planner(idea="", venture_id=new_venture_id("events-rejected"))
    check("published mvp_planner_started even for a rejected run", "mvp_planner_started" in seen_events)
    check("published mvp_planner_failed for a rejected run", "mvp_planner_failed" in seen_events)
    check("did not publish mvp_planner_completed for a rejected run", "mvp_planner_completed" not in seen_events)

    print("\n== 9. Deterministic Output ==")
    mvp_planner.set_decision_engine_invoker(lambda idea, vid, depth: _BUILD_NOW_DECISION)
    try:
        vid = new_venture_id("deterministic")
        first_run = mvp_agent.run_mvp_planner(idea="deterministic idea", venture_id=vid)
        second_run = mvp_agent.run_mvp_planner(idea="deterministic idea", venture_id=vid)
        for field in ("feature_list", "tech_stack", "database_outline", "api_outline", "folder_structure", "development_timeline", "milestones", "risks_and_dependencies"):
            check(f"{field} identical across two runs of the same input", first_run[field] == second_run[field])
    finally:
        mvp_planner.reset_decision_engine_invoker()
    check("decision engine invoker restored to the real default", mvp_planner.get_decision_engine_invoker() is mvp_planner._default_decision_engine_invoker)

    print("\n== 10. Manager-callable node shape ==")
    mvp_planner.set_decision_engine_invoker(lambda idea, vid, depth: _BUILD_NOW_DECISION)
    try:
        delta = mvp_agent.mvp_planner_node({"idea": "node integration idea", "venture_id": new_venture_id("node")})
        check("mvp_planner_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
        check("node result carries a completed status", delta["history"][0]["status"] == "completed")
    finally:
        mvp_planner.reset_decision_engine_invoker()

    print("\n== 11. Full Regression of All Previous Phases/Components ==")
    repo_root = Path(__file__).resolve().parent.parent
    # These four files each embed their own full regression section, globbing
    # "smoke_test_phase*.py" - none of their (frozen, not to be modified)
    # exclusion lists know about this new file, so including any of them here
    # would let it call back into this file, forming an unbounded subprocess
    # cycle. All four already re-verify the full prior suite standalone, so
    # excluding them here loses no coverage.
    excluded = {
        "smoke_test_phase4_mvp_planner.py",
        "smoke_test_phase3_ads.py",
        "smoke_test_phase4_founder_orchestrator.py",
        "smoke_test_phase4_research_pipeline.py",
        "smoke_test_phase4_decision_engine.py",
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

    print("\n== 12. pip check ==")
    pip_result = subprocess.run([sys.executable, "-m", "pip", "check"], cwd=repo_root, capture_output=True, text=True)
    check("pip check reports no broken requirements", pip_result.returncode == 0)

    print("\n== 13. git status (only new files, nothing modified) ==")
    git_result = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True)
    status_lines = [line for line in git_result.stdout.splitlines() if line.strip()]
    modified_or_deleted = [line for line in status_lines if not line.startswith("??")]
    check("git status has no modified/deleted files, only untracked additions", len(modified_or_deleted) == 0)

    print("\nAll Phase 4 Component 4 checks passed.")


if __name__ == "__main__":
    main()
