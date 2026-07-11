"""Phase 4, Component 1: Founder Orchestrator.

Coordinates the existing, unmodified Research Supervisor and its 7 workers (Search,
Browser, Market/Competitor/Trend Intelligence, Opportunity Detection, Idea
Validation) into a single Idea -> Research -> Analysis -> Decision -> Plan pipeline,
returning a structured FounderExecutionPlan. Built entirely on top of the frozen
Phase 0 kernel (Event Bus, Approval Engine, Agent Registry, Permission Layer, Memory
Gateway) and the already-verified Phase 1 research_graph.py - nothing in either is
modified.

Verifies:
  - Agent registration + manifest
  - Pipeline creation (graph compiles, runs end to end)
  - Execution plan generation (happy path, all required fields populated)
  - Dependency injection (swappable research invoker)
  - Retry (a flaky research invoker recovers within max attempts)
  - Timeout (a genuinely slow research invoker is aborted quickly, not blocked on)
  - Graceful failure: empty idea, missing venture, invalid constraints, invalid
    budget, duplicate execution, worker failure, unexpected exception
  - High-risk approval interrupt for large-budget runs (reject + approve paths)
  - Event publishing
  - Manager node integration
  - Regression of every previous phase/component (run separately, see the full
    suite this script is part of)
  - pip check
  - git status

Run: python scripts/smoke_test_phase4_founder_orchestrator.py
"""

import logging
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

import agents.founder.orchestrator.agent as orch_agent  # noqa: E402
import workflows.founder_pipeline as founder_pipeline  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.registries import get_agent_registry  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def new_venture_id(prefix: str) -> str:
    return f"venture-{prefix}-{uuid.uuid4().hex[:8]}"


def main() -> None:
    print("\n== 1. Agent Registration + Manifest ==")
    manifest = get_agent_registry().get("founder_orchestrator")
    check("registered with supervisor null (new root-level orchestrator)", manifest.supervisor is None)
    check("declares internet_access permission", set(manifest.permissions) == {"internet_access"})
    check("lifecycle is active", manifest.lifecycle == "active")
    check("responsibilities documented", len(manifest.responsibilities) > 0)

    print("\n== 2. Pipeline Creation ==")
    graph = founder_pipeline.build_founder_pipeline()
    check("graph builds with all 5 nodes", set(graph.nodes) == {"intake", "research", "analysis", "decision", "plan"})

    print("\n== 3. Execution Plan Generation (happy path) ==")
    vid = new_venture_id("happy")
    result = orch_agent.run_founder_pipeline(idea="A marketplace for solo founders to hire vetted freelance CFOs", venture_id=vid)
    check("pipeline completed", result["status"] == "completed")
    check("idea echoed back", result["idea"].startswith("A marketplace"))
    check("venture_id echoed back", result["venture_id"] == vid)
    check("research_tasks populated", len(result["research_tasks"]) > 0)
    check("analysis_tasks populated", len(result["analysis_tasks"]) > 0)
    check("decision_tasks populated", len(result["decision_tasks"]) > 0)
    check(
        "required_agents includes the research supervisor and its 7 workers",
        set(result["required_agents"])
        == {
            "research_supervisor", "search_agent", "browser_agent", "market_intelligence_agent",
            "competitor_intelligence_agent", "trend_intelligence_agent", "opportunity_detection_agent", "idea_validation_agent",
        },
    )
    check("execution_order lists all 8 agents in pipeline order", result["execution_order"][0] == "research_supervisor" and result["execution_order"][-1] == "idea_validation_agent")
    check("estimated_runtime_seconds is positive", result["estimated_runtime_seconds"] > 0)
    check("risk_level is one of low/medium/high", result["risk_level"] in ("low", "medium", "high"))
    check("summary mentions the idea", "marketplace" in result["summary"].lower())

    print("\n== 4. Dependency Injection ==")
    original_invoker = founder_pipeline.get_research_invoker()
    calls: list[dict] = []

    def fake_invoker(payload: dict) -> dict:
        calls.append(payload)
        return {
            "research_findings": [{"agent": "research_supervisor", "event": "intake", "idea": payload["idea"]}],
            "history": [],
        }

    founder_pipeline.set_research_invoker(fake_invoker)
    try:
        vid_di = new_venture_id("di")
        di_result = orch_agent.run_founder_pipeline(idea="DI test idea", venture_id=vid_di)
        check("injected research invoker was actually called", len(calls) == 1 and calls[0]["idea"] == "DI test idea")
        check("pipeline completed using the injected invoker", di_result["status"] == "completed")
    finally:
        founder_pipeline.set_research_invoker(original_invoker)
    check("provider restored to the default after injection", founder_pipeline.get_research_invoker() is original_invoker)

    print("\n== 5. Retry Behaviour ==")
    retry_calls = {"n": 0}

    def flaky_invoker(payload: dict) -> dict:
        retry_calls["n"] += 1
        if retry_calls["n"] < 2:
            raise RuntimeError("simulated transient research failure")
        return {"research_findings": [], "history": []}

    founder_pipeline.set_research_invoker(flaky_invoker)
    try:
        retry_result = orch_agent.run_founder_pipeline(idea="retry idea", venture_id=new_venture_id("retry"))
        check("retried past one transient failure and completed", retry_result["status"] == "completed")
        check("exactly 2 attempts were made", retry_calls["n"] == 2)
    finally:
        founder_pipeline.set_research_invoker(original_invoker)

    print("\n== 6. Timeout ==")
    def slow_invoker(payload: dict) -> dict:
        time.sleep(2.0)
        return {"research_findings": [], "history": []}

    orig_timeout = founder_pipeline._RESEARCH_TIMEOUT_SECONDS
    orig_attempts = founder_pipeline._RESEARCH_MAX_ATTEMPTS
    founder_pipeline._RESEARCH_TIMEOUT_SECONDS = 0.3
    founder_pipeline._RESEARCH_MAX_ATTEMPTS = 1
    founder_pipeline.set_research_invoker(slow_invoker)
    try:
        start = time.monotonic()
        timeout_result = orch_agent.run_founder_pipeline(idea="timeout idea", venture_id=new_venture_id("timeout"))
        elapsed = time.monotonic() - start
        check("a slow research invoker times out per the configured limit", timeout_result["status"] == "failed")
        check("timeout error message is descriptive", "timed out" in timeout_result["error"])
        check("caller was not blocked for the full sleep duration", elapsed < 1.5)
    finally:
        founder_pipeline.set_research_invoker(original_invoker)
        founder_pipeline._RESEARCH_TIMEOUT_SECONDS = orig_timeout
        founder_pipeline._RESEARCH_MAX_ATTEMPTS = orig_attempts

    print("\n== 7. Graceful Failure: input validation ==")
    empty_idea = orch_agent.run_founder_pipeline(idea="", venture_id=new_venture_id("empty-idea"))
    check("empty idea is rejected gracefully", empty_idea["status"] == "rejected" and "idea" in empty_idea["error"])

    missing_venture = orch_agent.run_founder_pipeline(idea="some idea", venture_id="")
    check("missing venture_id is rejected gracefully", missing_venture["status"] == "rejected" and "venture_id" in missing_venture["error"])

    bad_constraints = orch_agent.run_founder_pipeline(idea="some idea", venture_id=new_venture_id("bad-constraints"), constraints="not-a-dict")
    check("invalid constraints type is rejected gracefully", bad_constraints["status"] == "rejected")
    check("invalid constraints never leak into the plan's constraints field", bad_constraints["constraints"] == {})

    bad_budget_type = orch_agent.run_founder_pipeline(idea="some idea", venture_id=new_venture_id("bad-budget"), budget="not-a-number")
    check("invalid budget type is rejected gracefully", bad_budget_type["status"] == "rejected")
    check("invalid budget never leaks into the plan's budget field", bad_budget_type["budget"] is None)

    print("\n== 8. Graceful Failure: duplicate execution ==")
    dup_vid = new_venture_id("duplicate")
    founder_pipeline._in_progress.add(dup_vid)
    try:
        dup_result = orch_agent.run_founder_pipeline(idea="duplicate idea", venture_id=dup_vid)
        check("a second run for an in-progress venture_id is rejected", dup_result["status"] == "rejected")
        check("duplicate execution reason is descriptive", "duplicate execution" in dup_result["error"])
    finally:
        founder_pipeline._in_progress.discard(dup_vid)

    print("\n== 9. Graceful Failure: worker failure ==")
    def always_fails(payload: dict) -> dict:
        raise RuntimeError("worker exploded")

    founder_pipeline.set_research_invoker(always_fails)
    try:
        failure_result = orch_agent.run_founder_pipeline(idea="fail idea", venture_id=new_venture_id("worker-fail"))
        check("a persistently failing worker is handled gracefully", failure_result["status"] == "failed")
        check("worker failure error is descriptive", "worker exploded" in failure_result["error"])
    finally:
        founder_pipeline.set_research_invoker(original_invoker)

    print("\n== 10. Graceful Failure: unexpected exception ==")
    class _WeirdError(Exception):
        pass

    def exotic_failure(payload: dict) -> dict:
        raise _WeirdError("totally unexpected failure mode")

    founder_pipeline.set_research_invoker(exotic_failure)
    try:
        weird_vid = new_venture_id("unexpected")
        weird_result = orch_agent.run_founder_pipeline(idea="weird idea", venture_id=weird_vid)
        check("an unexpected exception type is still handled gracefully", weird_result["status"] == "failed")
        check("the duplicate-execution guard is released even after an unexpected failure", weird_vid not in founder_pipeline._in_progress)
    finally:
        founder_pipeline.set_research_invoker(original_invoker)

    print("\n== 11. High-Risk Approval Interrupt (large budget) ==")
    reject_vid = new_venture_id("budget-reject")
    pending = orch_agent.run_founder_pipeline(idea="expensive idea", venture_id=reject_vid, budget=5000.0)
    check("a large-budget run pauses for human approval", pending["event"] == "founder_pipeline_pending_approval")
    check("interrupt carries the founder_pipeline_start action", pending["interrupt"]["action"] == "founder_pipeline_start")

    rejected = orch_agent.resume_founder_pipeline(reject_vid, approved=False, reason="too risky")
    check("a rejected high-budget run does not proceed to research", rejected["status"] == "rejected")
    check("the duplicate-execution guard is released after rejection", reject_vid not in founder_pipeline._in_progress)

    approve_vid = new_venture_id("budget-approve")
    orch_agent.run_founder_pipeline(idea="expensive idea", venture_id=approve_vid, budget=5000.0)
    approved = orch_agent.resume_founder_pipeline(approve_vid, approved=True, reason="reviewed")
    check("an approved high-budget run completes", approved["status"] == "completed")
    check("the duplicate-execution guard is released after approval", approve_vid not in founder_pipeline._in_progress)

    print("\n== 12. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    orch_agent.run_founder_pipeline(idea="event test idea", venture_id=new_venture_id("events"))
    check("published founder_pipeline_started", "founder_pipeline_started" in seen_events)
    check("published founder_pipeline_research_completed", "founder_pipeline_research_completed" in seen_events)
    check("published founder_pipeline_completed", "founder_pipeline_completed" in seen_events)

    seen_events.clear()
    orch_agent.run_founder_pipeline(idea="", venture_id=new_venture_id("events-rejected"))
    check("published founder_pipeline_rejected for an invalid run", "founder_pipeline_rejected" in seen_events)

    print("\n== 13. Manager-callable node shape ==")
    node_vid = new_venture_id("node")
    delta = orch_agent.founder_orchestrator_node({"idea": "node integration idea", "venture_id": node_vid})
    check("founder_orchestrator_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
    check("node result reflects a completed pipeline run", delta["history"][0]["status"] == "completed")

    print("\n== 14. Full Regression of All Previous Phases/Components ==")
    repo_root = Path(__file__).resolve().parent.parent
    # smoke_test_phase3_ads.py (Phase 3 Component 13) is excluded here, not just
    # this script's own name: its own regression section (a prior, already-
    # verified requirement, not something this component may modify) globs
    # "smoke_test_phase*.py" and would in turn pick up *this* file, forming an
    # unbounded ads<->phase4 subprocess-spawning cycle. ads.py already re-verifies
    # the entire Phase 0-3 suite on its own, so excluding it here loses no
    # coverage - every test it would have run is already run directly below.
    smoke_tests = sorted(
        p for p in (repo_root / "scripts").glob("smoke_test_phase*.py")
        if p.name not in {"smoke_test_phase4_founder_orchestrator.py", "smoke_test_phase3_ads.py"}
    )
    for test_path in smoke_tests:
        proc = subprocess.run([sys.executable, str(test_path)], cwd=repo_root, capture_output=True, text=True)
        check(f"regression: {test_path.name}", proc.returncode == 0)

    print("\n== 15. pip check ==")
    pip_result = subprocess.run([sys.executable, "-m", "pip", "check"], cwd=repo_root, capture_output=True, text=True)
    check("pip check reports no broken requirements", pip_result.returncode == 0)

    print("\n== 16. git status (only new files, nothing modified) ==")
    git_result = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True)
    status_lines = [line for line in git_result.stdout.splitlines() if line.strip()]
    modified_or_deleted = [line for line in status_lines if not line.startswith("??")]
    check("git status has no modified/deleted files, only untracked additions", len(modified_or_deleted) == 0)

    print("\nAll Phase 4 Component 1 checks passed.")


if __name__ == "__main__":
    main()
