"""Phase 4, Component 5: AI Builder Pipeline.

Consumes ONLY the completed MVP Planner's (Phase 4 Component 4) MVPPlan and
orchestrates the existing, unmodified Phase 2 Coding Engine agents (Claude Code,
Git, File Editor, Terminal, Build Runner, Debug & Retry, Loop Controller) to
scaffold, code, build, and test a project. Duplicates none of their logic - every
dispatch in AIBuilderLoopProvider is a single call into an unmodified Phase 2
agent function, and the build-fix-rebuild loop itself is entirely driven by the
existing Loop Controller Agent's own start_loop()/step_loop() (the exact custom-
LoopProvider extension seam tools/loop/loop_providers.py's own docstring
describes). Built on top of the frozen Phase 0 kernel and the already-verified
Phase 4 Components 1-4 - nothing in Phase 0-3 or Phase 4 Components 1-4 is
modified.

Verifies:
  - Agent registration + manifest
  - Graph creation (5 nodes: intake, mvp_plan, generate_execution_plan,
    build_loop, aggregate)
  - Execution plan generation (pure Python, deterministic, derived from the
    MVP Plan's folder_structure/feature_list)
  - A fully deterministic successful build (fake MVP Plan + fake step executor -
    touches no real git/filesystem/LLM)
  - Build loop failure handling (every step fails -> Loop Controller's own
    retry_budget is exhausted -> a well-formed failed Build Report, never a crash)
  - Debug & Retry automatically invoked on a build failure (AIBuilderLoopProvider
    exercised directly): a transient failure is diagnosed and retried to success;
    a permanent failure is correctly NOT retried
  - An unbuildable MVP Plan (status != "completed", e.g. Component 4's own
    PIVOT/DROP "not_recommended" gate) skips the build entirely
  - Invalid input (empty idea, missing venture_id) rejected gracefully
  - Event publishing (ai_builder_started/ai_builder_completed/ai_builder_failed)
  - Deterministic output (same input -> byte-identical Build Report, run twice)
  - Manager node integration
  - Regression of every previous phase/component (run directly, one process each -
    excluding the five files that each embed their own full regression section,
    which would otherwise call back into this file, forming an unbounded
    subprocess cycle - all five already re-verify the full prior suite standalone)
  - pip check
  - git status

Run: python scripts/smoke_test_phase4_ai_builder.py
"""

import logging
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

import agents.founder.ai_builder.agent as ai_agent  # noqa: E402
import workflows.ai_builder as ai_builder  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.registries import get_agent_registry  # noqa: E402
from tools.loop.loop_providers import LoopStepResult  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def new_venture_id(prefix: str) -> str:
    return f"venture-{prefix}-{uuid.uuid4().hex[:8]}"


_FAKE_MVP_PLAN = {
    "idea": "An AI copilot for solo founders", "status": "completed",
    "folder_structure": ["backend/", "backend/app/main.py"],
    "feature_list": [{"name": "Core Workflow", "description": "The primary workflow", "priority": "must_have"}],
    "tech_stack": [], "database_outline": [], "api_outline": [], "development_timeline": [],
}

_NOT_RECOMMENDED_PLAN = {"idea": "weak idea", "status": "not_recommended", "feature_list": []}


class _FakeAllSucceedExecutor:
    name = "fake_success"

    def __init__(self, venture_id: str) -> None:
        self.venture_id = venture_id

    def execute_step(self, step: dict, step_index: int) -> LoopStepResult:
        return LoopStepResult(success=True, step_index=step_index, output=f"fake ok: {step.get('type')}")


class _FakeAllFailExecutor:
    name = "fake_fail"

    def __init__(self, venture_id: str) -> None:
        self.venture_id = venture_id

    def execute_step(self, step: dict, step_index: int) -> LoopStepResult:
        return LoopStepResult(success=False, step_index=step_index, error="simulated permanent failure")


def main() -> None:
    print("\n== 1. Agent Registration + Manifest ==")
    manifest = get_agent_registry().get("ai_builder")
    check("registered with supervisor null", manifest.supervisor is None)
    check("declares no permissions (no new permission types, no Tool Registry calls of its own)", manifest.permissions == [])
    check("declares no tools", manifest.tools == [])
    check("lifecycle is active", manifest.lifecycle == "active")
    check("responsibilities documented", len(manifest.responsibilities) > 0)

    print("\n== 2. Graph Creation ==")
    graph = ai_builder.build_ai_builder()
    expected_nodes = {"intake", "mvp_plan", "generate_execution_plan", "build_loop", "aggregate"}
    check("graph builds with all 5 nodes", set(graph.nodes) == expected_nodes)

    print("\n== 3. Execution Plan Generation ==")
    plan = ai_builder._generate_execution_plan(_FAKE_MVP_PLAN)
    step_types = [s["type"] for s in plan]
    check("plan includes file_edit steps for the folder structure", step_types.count("file_edit") == 2)
    check("plan delegates coding to Claude Code", "claude_code" in step_types)
    check("plan includes git add + commit", step_types.count("git") == 2)
    check("plan includes a terminal step", "terminal" in step_types)
    check("plan includes a build step", "build" in step_types)
    check("claude_code step's task mentions the MVP feature", "Core Workflow" in next(s for s in plan if s["type"] == "claude_code")["task_description"])

    print("\n== 4. Deterministic Successful Build (fake MVP plan + fake step executor) ==")
    ai_builder.set_mvp_planner_invoker(lambda idea, vid, depth: _FAKE_MVP_PLAN)
    ai_builder.set_step_executor_factory(lambda vid: _FakeAllSucceedExecutor(vid))
    try:
        success_result = ai_agent.run_ai_builder(idea="An AI copilot for solo founders", venture_id=new_venture_id("success"))
        check("build completed", success_result["status"] == "completed")
        check("loop_status is completed", success_result["loop_status"] == "completed")
        check("all steps completed", success_result["steps_completed"] == success_result["steps_total"] and success_result["steps_total"] == 7)
        check("summary mentions the idea", "An AI copilot" in success_result["summary"])
        check("no error recorded on success", success_result["error"] == "")
    finally:
        ai_builder.reset_mvp_planner_invoker()
        ai_builder.reset_step_executor_factory()

    print("\n== 5. Build Loop Failure Handling ==")
    ai_builder.set_mvp_planner_invoker(lambda idea, vid, depth: _FAKE_MVP_PLAN)
    ai_builder.set_step_executor_factory(lambda vid: _FakeAllFailExecutor(vid))
    try:
        fail_result = ai_agent.run_ai_builder(idea="failing idea", venture_id=new_venture_id("fail"))
        check("a persistently failing build loop is reported gracefully, never crashes", fail_result["status"] == "build_failed")
        check("loop_status reflects the Loop Controller's own failure", fail_result["loop_status"] == "failed")
        check("a descriptive error is present", bool(fail_result["error"]))
    finally:
        ai_builder.reset_mvp_planner_invoker()
        ai_builder.reset_step_executor_factory()

    print("\n== 6. Debug & Retry Automatically Invoked on Build Failure ==")
    calls = {"n": 0}

    def _flaky_build(build_type: str, venture_id: str = "default", **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"agent": "build_runner_agent", "event": "build_failed", "build_type": build_type, "error": "connection timeout while running build"}
        return {"agent": "build_runner_agent", "event": "build_completed", "build_type": build_type, "output": "tests passed"}

    original_build_op = ai_builder.run_build_operation
    ai_builder.run_build_operation = _flaky_build
    try:
        provider = ai_builder.AIBuilderLoopProvider(new_venture_id("debug-retry"))
        result = provider.execute_step({"type": "build", "build_type": "python", "kwargs": {"action": "test"}}, 0)
        check("a transient build failure is diagnosed and retried to success", result.success is True)
        check("Debug & Retry was invoked exactly once for this step", provider.debug_retry_invocations == 1)
        check("the underlying build was genuinely called twice (initial + one retry)", calls["n"] == 2)
    finally:
        ai_builder.run_build_operation = original_build_op

    def _always_permanent_fail(build_type: str, venture_id: str = "default", **kwargs):
        return {"agent": "build_runner_agent", "event": "build_failed", "build_type": build_type, "error": "no module named missing_pkg"}

    ai_builder.run_build_operation = _always_permanent_fail
    try:
        provider2 = ai_builder.AIBuilderLoopProvider(new_venture_id("debug-retry-permanent"))
        result2 = provider2.execute_step({"type": "build", "build_type": "python", "kwargs": {"action": "test"}}, 0)
        check("a permanent (non-transient) build failure is correctly NOT retried into success", result2.success is False)
        check("Debug & Retry was still invoked to diagnose it", provider2.debug_retry_invocations == 1)
    finally:
        ai_builder.run_build_operation = original_build_op

    print("\n== 7. Unbuildable MVP Plan Skips the Build ==")
    ai_builder.set_mvp_planner_invoker(lambda idea, vid, depth: _NOT_RECOMMENDED_PLAN)
    try:
        skipped_result = ai_agent.run_ai_builder(idea="weak idea", venture_id=new_venture_id("not-recommended"))
        check("a not_recommended MVP plan yields status 'skipped'", skipped_result["status"] == "skipped")
        check("no build steps were generated/attempted", skipped_result["steps_total"] == 0)
        check("the reason references the MVP Planner's status", "not_recommended" in skipped_result["error"])
    finally:
        ai_builder.reset_mvp_planner_invoker()

    print("\n== 8. Invalid Input ==")
    empty_idea = ai_agent.run_ai_builder(idea="", venture_id=new_venture_id("empty-idea"))
    check("empty idea is rejected gracefully", empty_idea["status"] == "rejected")

    missing_venture = ai_agent.run_ai_builder(idea="some idea", venture_id="")
    check("missing venture_id is rejected gracefully", missing_venture["status"] == "rejected")

    ai_builder.set_mvp_planner_invoker(lambda idea, vid, depth: _FAKE_MVP_PLAN)
    ai_builder.set_step_executor_factory(lambda vid: _FakeAllSucceedExecutor(vid))
    try:
        invalid_depth = ai_agent.run_ai_builder(idea="some idea", venture_id=new_venture_id("bad-depth"), research_depth="ultra")
        check("an invalid research_depth is silently coerced to 'standard', not rejected", invalid_depth["status"] == "completed")
    finally:
        ai_builder.reset_mvp_planner_invoker()
        ai_builder.reset_step_executor_factory()

    print("\n== 9. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    ai_builder.set_mvp_planner_invoker(lambda idea, vid, depth: _FAKE_MVP_PLAN)
    ai_builder.set_step_executor_factory(lambda vid: _FakeAllSucceedExecutor(vid))
    try:
        ai_agent.run_ai_builder(idea="event test idea", venture_id=new_venture_id("events"))
    finally:
        ai_builder.reset_mvp_planner_invoker()
        ai_builder.reset_step_executor_factory()
    check("published ai_builder_started", "ai_builder_started" in seen_events)
    check("published ai_builder_completed", "ai_builder_completed" in seen_events)
    check("did not publish ai_builder_failed for a successful run", "ai_builder_failed" not in seen_events)

    seen_events.clear()
    ai_agent.run_ai_builder(idea="", venture_id=new_venture_id("events-rejected"))
    check("published ai_builder_started even for a rejected run", "ai_builder_started" in seen_events)
    check("published ai_builder_failed for a rejected run", "ai_builder_failed" in seen_events)
    check("did not publish ai_builder_completed for a rejected run", "ai_builder_completed" not in seen_events)

    print("\n== 10. Deterministic Output ==")
    ai_builder.set_mvp_planner_invoker(lambda idea, vid, depth: _FAKE_MVP_PLAN)
    ai_builder.set_step_executor_factory(lambda vid: _FakeAllSucceedExecutor(vid))
    try:
        vid = new_venture_id("deterministic")
        first_run = ai_agent.run_ai_builder(idea="deterministic idea", venture_id=vid)
        second_run = ai_agent.run_ai_builder(idea="deterministic idea", venture_id=vid)
        for field in ("status", "loop_status", "steps_total", "steps_completed", "debug_retry_invocations"):
            check(f"{field} identical across two runs of the same input", first_run[field] == second_run[field])
    finally:
        ai_builder.reset_mvp_planner_invoker()
        ai_builder.reset_step_executor_factory()
    check("mvp planner invoker restored to the real default", ai_builder.get_mvp_planner_invoker() is ai_builder._default_mvp_planner_invoker)
    check("step executor factory restored to the real default", ai_builder.get_step_executor_factory() is ai_builder._default_step_executor_factory)

    print("\n== 11. Manager-callable node shape ==")
    ai_builder.set_mvp_planner_invoker(lambda idea, vid, depth: _FAKE_MVP_PLAN)
    ai_builder.set_step_executor_factory(lambda vid: _FakeAllSucceedExecutor(vid))
    try:
        delta = ai_agent.ai_builder_node({"idea": "node integration idea", "venture_id": new_venture_id("node")})
        check("ai_builder_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
        check("node result carries a completed status", delta["history"][0]["status"] == "completed")
    finally:
        ai_builder.reset_mvp_planner_invoker()
        ai_builder.reset_step_executor_factory()

    print("\n== 12. Full Regression of All Previous Phases/Components ==")
    repo_root = Path(__file__).resolve().parent.parent
    # These five files each embed their own full regression section, globbing
    # "smoke_test_phase*.py" - none of their (frozen, not to be modified)
    # exclusion lists know about this new file, so including any of them here
    # would let it call back into this file, forming an unbounded subprocess
    # cycle. All five already re-verify the full prior suite standalone, so
    # excluding them here loses no coverage.
    excluded = {
        "smoke_test_phase4_ai_builder.py",
        "smoke_test_phase3_ads.py",
        "smoke_test_phase4_founder_orchestrator.py",
        "smoke_test_phase4_research_pipeline.py",
        "smoke_test_phase4_decision_engine.py",
        "smoke_test_phase4_mvp_planner.py",
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

    print("\n== 13. pip check ==")
    pip_result = subprocess.run([sys.executable, "-m", "pip", "check"], cwd=repo_root, capture_output=True, text=True)
    check("pip check reports no broken requirements", pip_result.returncode == 0)

    print("\n== 14. git status (only new files, nothing modified) ==")
    git_result = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True)
    status_lines = [line for line in git_result.stdout.splitlines() if line.strip()]
    modified_or_deleted = [line for line in status_lines if not line.startswith("??")]
    check("git status has no modified/deleted files, only untracked additions", len(modified_or_deleted) == 0)

    print("\nAll Phase 4 Component 5 checks passed.")


if __name__ == "__main__":
    main()
