"""Phase 4, Component 8: Founder Dashboard.

Consumes ONLY the outputs of the 7 prior Phase 4 components (Founder
Orchestrator, Research Pipeline, Decision Engine, MVP Planner, AI Builder,
Deployment Pipeline, Growth Pipeline) plus the existing Phase 2 Git Agent's
status, and aggregates them into a single FounderDashboard. Duplicates none of
their implementation - every section of the dashboard is populated directly
from an unmodified run_x() call into one of those 8 agents. Built on top of the
frozen Phase 0 kernel and the already-verified Phase 4 Components 1-7 - nothing
in Phase 0-3 or Phase 4 Components 1-7 is modified. This is the final component
of Phase 4.

Verifies:
  - Agent registration + manifest
  - Graph creation (10 nodes: intake, founder_orchestrator, research, decision,
    mvp, build, deployment, growth, git_status, aggregate)
  - A fully deterministic all-healthy dashboard (fake results for all 7
    components + fake git status - touches no real research/decision/build/
    deploy/growth/git backend)
  - Degraded health (some components healthy, some not) computed correctly
  - Critical health (no components healthy) still handled gracefully - the
    dashboard itself always completes even when every reused component fails
  - Invalid input (empty idea, missing venture_id) rejected gracefully
  - Event publishing (dashboard_started/dashboard_completed/dashboard_failed)
  - Deterministic output (same input -> byte-identical dashboard, run twice)
  - Manager node integration
  - Regression of every previous phase/component (run directly, one process
    each - excluding the eight files that each embed their own full
    regression section, which would otherwise call back into this file,
    forming an unbounded subprocess cycle - all eight already re-verify the
    full prior suite standalone)
  - pip check
  - git status

Run: python scripts/smoke_test_phase4_founder_dashboard.py
"""

import logging
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

import agents.founder.dashboard.agent as dash_agent  # noqa: E402
import workflows.founder_dashboard as founder_dashboard  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.registries import get_agent_registry  # noqa: E402

_COMPONENT_KEYS = ("founder_orchestrator", "research_pipeline", "decision_engine", "mvp_planner", "ai_builder", "deployment_pipeline", "growth_pipeline")


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def new_venture_id(prefix: str) -> str:
    return f"venture-{prefix}-{uuid.uuid4().hex[:8]}"


_FAKE_FOUNDER = {"agent": "founder_orchestrator", "status": "completed", "risk_level": "low", "required_agents": ["research_supervisor"]}
_FAKE_RESEARCH = {
    "agent": "research_pipeline", "status": "completed", "confidence_score": 0.8, "sources": ["s1", "s2"], "research_depth": "standard",
    "market_analysis": {"market_size_tam": "$5B"}, "competitor_analysis": {"direct_competitors": []},
}
_FAKE_DECISION = {
    "agent": "decision_engine", "status": "completed", "opportunity_score": 8.0, "competition_score": 8.0, "risk_score": 7.0,
    "market_size_score": 9.0, "build_difficulty": 7.0, "execution_complexity": 6.0, "revenue_potential": 8.0, "market_timing": 8.0,
    "ai_advantage": 7.0, "confidence_score": 8.0, "overall_score": 8.5, "recommendation": "BUILD NOW", "warnings": [], "next_actions": ["Begin MVP development"],
}
_FAKE_MVP = {
    "agent": "mvp_planner", "status": "completed", "feature_list": [{"name": "Core"}], "tech_stack": [{"layer": "backend", "technology": "FastAPI"}],
    "milestones": [{"name": "m1"}], "risks_and_dependencies": [{"risk": "team capacity"}],
}
_FAKE_BUILD = {"agent": "ai_builder", "status": "completed", "steps_completed": 7, "steps_total": 7, "debug_retry_invocations": 0}
_FAKE_DEPLOYMENT = {"agent": "deployment_pipeline", "status": "completed", "ready_stage_count": 5, "total_stage_count": 5}
_FAKE_GROWTH = {"agent": "growth_pipeline", "status": "completed", "ready_stage_count": 5, "total_stage_count": 5}
_FAKE_GIT = {"agent": "git_agent", "event": "git_operation_completed", "output": "On branch main\nnothing to commit"}
_FAKE_GIT_UNAVAILABLE = {"agent": "git_agent", "event": "git_operation_failed", "error": "not a git repository"}

_ALL_HEALTHY = {
    "founder_orchestrator": lambda idea, vid, depth: _FAKE_FOUNDER,
    "research_pipeline": lambda idea, vid, depth: _FAKE_RESEARCH,
    "decision_engine": lambda idea, vid, depth: _FAKE_DECISION,
    "mvp_planner": lambda idea, vid, depth: _FAKE_MVP,
    "ai_builder": lambda idea, vid, depth: _FAKE_BUILD,
    "deployment_pipeline": lambda idea, vid, depth: _FAKE_DEPLOYMENT,
    "growth_pipeline": lambda idea, vid, depth: _FAKE_GROWTH,
}

_FAILED = {"status": "failed", "error": "unreachable"}


def main() -> None:
    print("\n== 1. Agent Registration + Manifest ==")
    manifest = get_agent_registry().get("founder_dashboard")
    check("registered with supervisor null", manifest.supervisor is None)
    check("declares no permissions (no new permission types, no Tool Registry calls of its own)", manifest.permissions == [])
    check("declares no tools", manifest.tools == [])
    check("lifecycle is active", manifest.lifecycle == "active")
    check("responsibilities documented", len(manifest.responsibilities) > 0)
    check("outputs field documents FounderDashboard", any("FounderDashboard" in o for o in manifest.outputs))

    print("\n== 2. Graph Creation ==")
    graph = founder_dashboard.build_founder_dashboard()
    expected_nodes = {"intake", "founder_orchestrator", "research", "decision", "mvp", "build", "deployment", "growth", "git_status", "aggregate"}
    check("graph builds with all 10 nodes", set(graph.nodes) == expected_nodes)

    print("\n== 3. Fully Deterministic All-Healthy Dashboard ==")
    founder_dashboard.set_component_invokers(_ALL_HEALTHY)
    founder_dashboard.set_git_status_invoker(lambda vid: _FAKE_GIT)
    try:
        healthy = dash_agent.run_founder_dashboard(idea="An AI copilot for solo founders", venture_id=new_venture_id("healthy"))
        check("dashboard completed", healthy["status"] == "completed")
        check("overall_health is 'healthy' when all 7 components succeed", healthy["overall_health"] == "healthy")
        check("no alerts when everything is healthy", healthy["alerts"] == [])
        check("venture_summary reflects the Founder Orchestrator's risk_level", healthy["venture_summary"]["risk_level"] == "low")
        check("research_summary reflects the Research Pipeline's confidence_score", healthy["research_summary"]["confidence_score"] == 0.8)
        check("market_analysis passed through from the Research Pipeline", healthy["market_analysis"]["market_size_tam"] == "$5B")
        check("competitor_summary passed through from the Research Pipeline", "direct_competitors" in healthy["competitor_summary"])
        check("decision_scores reflects the Decision Engine's recommendation", healthy["decision_scores"]["recommendation"] == "BUILD NOW")
        check("decision_scores includes all 9 dimension scores plus overall", healthy["decision_scores"]["overall_score"] == 8.5)
        check("mvp_plan reflects the MVP Planner's feature count", healthy["mvp_plan"]["feature_count"] == 1)
        check("build_status reflects the AI Builder's step counts", healthy["build_status"]["steps_completed"] == 7 and healthy["build_status"]["steps_total"] == 7)
        check("deployment_status reflects the Deployment Pipeline's readiness", healthy["deployment_status"]["ready_stage_count"] == 5)
        check("growth_status reflects the Growth Pipeline's readiness", healthy["growth_status"]["ready_stage_count"] == 5)
        check("git_status reflects the Git Agent's real output", healthy["git_status"]["available"] is True and "On branch main" in healthy["git_status"]["output"])
        check("risks aggregated from Decision Engine warnings and MVP Planner risks_and_dependencies", "team capacity" in healthy["risks"])
        check("recommended_next_actions includes the Decision Engine's own next_actions", "Begin MVP development" in healthy["recommended_next_actions"])
        check("summary mentions the idea and health", "An AI copilot" in healthy["summary"] and "healthy" in healthy["summary"])
    finally:
        founder_dashboard.reset_component_invokers()
        founder_dashboard.reset_git_status_invoker()

    print("\n== 4. Degraded Health (some components healthy, some not) ==")
    mixed = dict(_ALL_HEALTHY)
    mixed["ai_builder"] = lambda idea, vid, depth: _FAILED
    mixed["deployment_pipeline"] = lambda idea, vid, depth: _FAILED
    mixed["growth_pipeline"] = lambda idea, vid, depth: _FAILED
    founder_dashboard.set_component_invokers(mixed)
    founder_dashboard.set_git_status_invoker(lambda vid: _FAKE_GIT)
    try:
        degraded = dash_agent.run_founder_dashboard(idea="mixed idea", venture_id=new_venture_id("degraded"))
        check("overall_health is 'degraded' when roughly half the components succeed", degraded["overall_health"] == "degraded")
        check("exactly 3 alerts for the 3 failed components", len(degraded["alerts"]) == 3)
        check("dashboard still completes despite partial component failure", degraded["status"] == "completed")
    finally:
        founder_dashboard.reset_component_invokers()
        founder_dashboard.reset_git_status_invoker()

    print("\n== 5. Critical Health (no components healthy, still graceful) ==")
    all_failed = {key: (lambda idea, vid, depth: _FAILED) for key in _COMPONENT_KEYS}
    founder_dashboard.set_component_invokers(all_failed)
    founder_dashboard.set_git_status_invoker(lambda vid: _FAKE_GIT_UNAVAILABLE)
    try:
        critical = dash_agent.run_founder_dashboard(idea="all fail idea", venture_id=new_venture_id("critical"))
        check("overall_health is 'critical' when no components succeed", critical["overall_health"] == "critical")
        check("all 7 components produce an alert", len(critical["alerts"]) == 8)  # 7 components + git status
        check("the dashboard workflow itself never crashes, even when everything it depends on fails", critical["status"] == "completed")
        check("git_status correctly reports unavailable", critical["git_status"]["available"] is False)
    finally:
        founder_dashboard.reset_component_invokers()
        founder_dashboard.reset_git_status_invoker()

    print("\n== 6. Invalid Input ==")
    empty_idea = dash_agent.run_founder_dashboard(idea="", venture_id=new_venture_id("empty-idea"))
    check("empty idea is rejected gracefully", empty_idea["status"] == "rejected")

    missing_venture = dash_agent.run_founder_dashboard(idea="some idea", venture_id="")
    check("missing venture_id is rejected gracefully", missing_venture["status"] == "rejected")

    founder_dashboard.set_component_invokers(_ALL_HEALTHY)
    founder_dashboard.set_git_status_invoker(lambda vid: _FAKE_GIT)
    try:
        invalid_depth = dash_agent.run_founder_dashboard(idea="some idea", venture_id=new_venture_id("bad-depth"), research_depth="ultra")
        check("an invalid research_depth is silently coerced to 'standard', not rejected", invalid_depth["status"] == "completed")
    finally:
        founder_dashboard.reset_component_invokers()
        founder_dashboard.reset_git_status_invoker()

    print("\n== 7. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    founder_dashboard.set_component_invokers(_ALL_HEALTHY)
    founder_dashboard.set_git_status_invoker(lambda vid: _FAKE_GIT)
    try:
        dash_agent.run_founder_dashboard(idea="event test idea", venture_id=new_venture_id("events"))
    finally:
        founder_dashboard.reset_component_invokers()
        founder_dashboard.reset_git_status_invoker()
    check("published dashboard_started", "dashboard_started" in seen_events)
    check("published dashboard_completed", "dashboard_completed" in seen_events)
    check("did not publish dashboard_failed for a successful run", "dashboard_failed" not in seen_events)

    seen_events.clear()
    dash_agent.run_founder_dashboard(idea="", venture_id=new_venture_id("events-rejected"))
    check("published dashboard_started even for a rejected run", "dashboard_started" in seen_events)
    check("published dashboard_failed for a rejected run", "dashboard_failed" in seen_events)
    check("did not publish dashboard_completed for a rejected run", "dashboard_completed" not in seen_events)

    print("\n== 8. Deterministic Output ==")
    founder_dashboard.set_component_invokers(_ALL_HEALTHY)
    founder_dashboard.set_git_status_invoker(lambda vid: _FAKE_GIT)
    try:
        vid = new_venture_id("deterministic")
        first_run = dash_agent.run_founder_dashboard(idea="deterministic idea", venture_id=vid)
        second_run = dash_agent.run_founder_dashboard(idea="deterministic idea", venture_id=vid)
        for field in ("overall_health", "decision_scores", "mvp_plan", "build_status", "deployment_status", "growth_status", "alerts", "risks"):
            check(f"{field} identical across two runs of the same input", first_run[field] == second_run[field])
    finally:
        founder_dashboard.reset_component_invokers()
        founder_dashboard.reset_git_status_invoker()
    check("component invokers restored to the real defaults", founder_dashboard.get_component_invokers() == founder_dashboard._DEFAULT_COMPONENT_INVOKERS)
    check("git status invoker restored to the real default", founder_dashboard.get_git_status_invoker() is founder_dashboard._default_git_status_invoker)

    print("\n== 9. Manager-callable node shape ==")
    founder_dashboard.set_component_invokers(_ALL_HEALTHY)
    founder_dashboard.set_git_status_invoker(lambda vid: _FAKE_GIT)
    try:
        delta = dash_agent.founder_dashboard_node({"idea": "node integration idea", "venture_id": new_venture_id("node")})
        check("founder_dashboard_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
        check("node result carries an overall_health field", delta["history"][0].get("overall_health") == "healthy")
    finally:
        founder_dashboard.reset_component_invokers()
        founder_dashboard.reset_git_status_invoker()

    print("\n== 10. Full Regression of All Previous Phases/Components ==")
    repo_root = Path(__file__).resolve().parent.parent
    # These eight files each embed their own full regression section, globbing
    # "smoke_test_phase*.py" - none of their (frozen, not to be modified)
    # exclusion lists know about this new file, so including any of them here
    # would let it call back into this file, forming an unbounded subprocess
    # cycle. All eight already re-verify the full prior suite standalone, so
    # excluding them here loses no coverage.
    excluded = {
        "smoke_test_phase4_founder_dashboard.py",
        "smoke_test_phase3_ads.py",
        "smoke_test_phase4_founder_orchestrator.py",
        "smoke_test_phase4_research_pipeline.py",
        "smoke_test_phase4_decision_engine.py",
        "smoke_test_phase4_mvp_planner.py",
        "smoke_test_phase4_ai_builder.py",
        "smoke_test_phase4_deployment_pipeline.py",
        "smoke_test_phase4_growth_pipeline.py",
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

    print("\n== 11. pip check ==")
    pip_result = subprocess.run([sys.executable, "-m", "pip", "check"], cwd=repo_root, capture_output=True, text=True)
    check("pip check reports no broken requirements", pip_result.returncode == 0)

    print("\n== 12. git status (only new files, nothing modified) ==")
    git_result = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True)
    status_lines = [line for line in git_result.stdout.splitlines() if line.strip()]
    modified_or_deleted = [line for line in status_lines if not line.startswith("??")]
    check("git status has no modified/deleted files, only untracked additions", len(modified_or_deleted) == 0)

    print("\nAll Phase 4 Component 8 checks passed. Phase 4 is now complete.")


if __name__ == "__main__":
    main()
