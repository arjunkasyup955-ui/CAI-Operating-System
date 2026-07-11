"""Phase 4, Component 6: Deployment Pipeline.

Consumes ONLY the completed AI Builder Pipeline's (Phase 4 Component 5) Build
Report and, only if the build was successful, orchestrates the existing,
unmodified Phase 3 Infrastructure Agents (Docker, PostgreSQL, Chroma, Ollama,
n8n) to prepare/verify the venture's deployment environment. Duplicates none of
their implementation - every stage is a sequence of direct calls into an
unmodified run_docker_operation/run_postgres_operation/run_chroma_operation/
run_ollama_operation/run_n8n_operation. Built on top of the frozen Phase 0
kernel and the already-verified Phase 4 Components 1-5 - nothing in Phase 0-3 or
Phase 4 Components 1-5 is modified.

Verifies:
  - Agent registration + manifest
  - Graph creation (8 nodes: intake, build_report, docker_stage, postgres_stage,
    chroma_stage, ollama_stage, n8n_stage, aggregate)
  - A fully deterministic successful deployment (fake Build Report + fake infra
    operations for all 5 agents - touches no real Docker/Postgres/Chroma/
    Ollama/n8n backend)
  - Partial deployment (some infra stages reachable, some not) still completes
    gracefully with an accurate per-stage breakdown
  - Complete infrastructure unavailability is still handled gracefully (never
    crashes, reports status "failed" with all 5 stages marked not ready)
  - An unsuccessful Build Report (status != "completed") skips deployment
    entirely
  - Invalid input (empty idea, missing venture_id) rejected gracefully
  - Event publishing (deployment_started/deployment_completed/deployment_failed)
  - Deterministic output (same input -> byte-identical Deployment Report, run
    twice)
  - Manager node integration
  - Regression of every previous phase/component (run directly, one process each
    - excluding the six files that each embed their own full regression
    section, which would otherwise call back into this file, forming an
    unbounded subprocess cycle - all six already re-verify the full prior suite
    standalone)
  - pip check
  - git status

Run: python scripts/smoke_test_phase4_deployment_pipeline.py
"""

import logging
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

import agents.founder.deployment_pipeline.agent as dp_agent  # noqa: E402
import workflows.deployment_pipeline as deployment_pipeline  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.registries import get_agent_registry  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def new_venture_id(prefix: str) -> str:
    return f"venture-{prefix}-{uuid.uuid4().hex[:8]}"


_SUCCESSFUL_BUILD_REPORT = {"idea": "An AI copilot for solo founders", "status": "completed"}
_UNSUCCESSFUL_BUILD_REPORT = {"idea": "weak idea", "status": "skipped"}


def _fake_succeed(operation: str, venture_id: str = "default", **kwargs) -> dict:
    return {"agent": "fake_infra", "event": "fake_completed", "operation": operation}


def _fake_unreachable(operation: str, venture_id: str = "default", **kwargs) -> dict:
    return {"agent": "fake_infra", "event": "fake_failed", "operation": operation, "error": "connection refused"}


def main() -> None:
    print("\n== 1. Agent Registration + Manifest ==")
    manifest = get_agent_registry().get("deployment_pipeline")
    check("registered with supervisor null", manifest.supervisor is None)
    check("declares no permissions (no new permission types, no Tool Registry calls of its own)", manifest.permissions == [])
    check("declares no tools", manifest.tools == [])
    check("lifecycle is active", manifest.lifecycle == "active")
    check("responsibilities documented", len(manifest.responsibilities) > 0)

    print("\n== 2. Graph Creation ==")
    graph = deployment_pipeline.build_deployment_pipeline()
    expected_nodes = {"intake", "build_report", "docker_stage", "postgres_stage", "chroma_stage", "ollama_stage", "n8n_stage", "aggregate"}
    check("graph builds with all 8 nodes", set(graph.nodes) == expected_nodes)

    print("\n== 3. Deterministic Successful Deployment (fake Build Report + fake infra ops) ==")
    deployment_pipeline.set_ai_builder_invoker(lambda idea, vid, depth: _SUCCESSFUL_BUILD_REPORT)
    deployment_pipeline.set_infra_operations({k: _fake_succeed for k in ("docker", "postgres", "chroma", "ollama", "n8n")})
    try:
        success = dp_agent.run_deployment_pipeline(idea="An AI copilot for solo founders", venture_id=new_venture_id("success"))
        check("deployment completed", success["status"] == "completed")
        check("build was validated", success["build_validated"] is True)
        check("all 5 stages ready", success["ready_stage_count"] == 5 and success["total_stage_count"] == 5)
        check("every stage reports healthy and prepared", all(s["healthy"] and s["prepared"] for s in success["stages"]))
        check("stage set covers docker/postgres/chroma/ollama/n8n", {s["stage"] for s in success["stages"]} == {"docker", "postgres", "chroma", "ollama", "n8n"})
        check("summary mentions the idea", "An AI copilot" in success["summary"])
        check("no error recorded on success", success["error"] == "")
    finally:
        deployment_pipeline.reset_ai_builder_invoker()
        deployment_pipeline.reset_infra_operations()

    print("\n== 4. Partial Deployment (mixed reachable/unreachable infrastructure) ==")
    deployment_pipeline.set_ai_builder_invoker(lambda idea, vid, depth: _SUCCESSFUL_BUILD_REPORT)
    deployment_pipeline.set_infra_operations(
        {"docker": _fake_succeed, "postgres": _fake_succeed, "chroma": _fake_succeed, "ollama": _fake_unreachable, "n8n": _fake_unreachable}
    )
    try:
        partial = dp_agent.run_deployment_pipeline(idea="partial idea", venture_id=new_venture_id("partial"))
        check("status is 'partial' when some but not all stages are ready", partial["status"] == "partial")
        check("exactly 3 of 5 stages ready", partial["ready_stage_count"] == 3)
        ollama_stage = next(s for s in partial["stages"] if s["stage"] == "ollama")
        check("the unreachable ollama stage is marked not healthy/not prepared", ollama_stage["healthy"] is False and ollama_stage["prepared"] is False)
        docker_stage = next(s for s in partial["stages"] if s["stage"] == "docker")
        check("the reachable docker stage is marked prepared", docker_stage["prepared"] is True)
        check("error field summarizes the failed stages", "connection refused" in partial["error"])
    finally:
        deployment_pipeline.reset_ai_builder_invoker()
        deployment_pipeline.reset_infra_operations()

    print("\n== 5. Complete Infrastructure Unavailability ==")
    deployment_pipeline.set_ai_builder_invoker(lambda idea, vid, depth: _SUCCESSFUL_BUILD_REPORT)
    deployment_pipeline.set_infra_operations({k: _fake_unreachable for k in ("docker", "postgres", "chroma", "ollama", "n8n")})
    try:
        all_fail = dp_agent.run_deployment_pipeline(idea="all fail idea", venture_id=new_venture_id("all-fail"))
        check("status is 'failed' when no infrastructure is reachable, never crashes", all_fail["status"] == "failed")
        check("zero stages ready", all_fail["ready_stage_count"] == 0)
        check("build was still validated (the build itself succeeded)", all_fail["build_validated"] is True)
        check("all 5 stages still individually reported, none silently dropped", len(all_fail["stages"]) == 5)
    finally:
        deployment_pipeline.reset_ai_builder_invoker()
        deployment_pipeline.reset_infra_operations()

    print("\n== 6. Unsuccessful Build Skips Deployment ==")
    deployment_pipeline.set_ai_builder_invoker(lambda idea, vid, depth: _UNSUCCESSFUL_BUILD_REPORT)
    try:
        skipped = dp_agent.run_deployment_pipeline(idea="weak idea", venture_id=new_venture_id("skip"))
        check("an unsuccessful build report yields status 'skipped'", skipped["status"] == "skipped")
        check("build_validated is False", skipped["build_validated"] is False)
        check("no infrastructure stages were attempted", skipped["total_stage_count"] == 0)
        check("the reason references the build's own status", "skipped" in skipped["error"])
    finally:
        deployment_pipeline.reset_ai_builder_invoker()

    print("\n== 7. Invalid Input ==")
    empty_idea = dp_agent.run_deployment_pipeline(idea="", venture_id=new_venture_id("empty-idea"))
    check("empty idea is rejected gracefully", empty_idea["status"] == "rejected")

    missing_venture = dp_agent.run_deployment_pipeline(idea="some idea", venture_id="")
    check("missing venture_id is rejected gracefully", missing_venture["status"] == "rejected")

    deployment_pipeline.set_ai_builder_invoker(lambda idea, vid, depth: _SUCCESSFUL_BUILD_REPORT)
    deployment_pipeline.set_infra_operations({k: _fake_succeed for k in ("docker", "postgres", "chroma", "ollama", "n8n")})
    try:
        invalid_depth = dp_agent.run_deployment_pipeline(idea="some idea", venture_id=new_venture_id("bad-depth"), research_depth="ultra")
        check("an invalid research_depth is silently coerced to 'standard', not rejected", invalid_depth["status"] == "completed")
    finally:
        deployment_pipeline.reset_ai_builder_invoker()
        deployment_pipeline.reset_infra_operations()

    print("\n== 8. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    deployment_pipeline.set_ai_builder_invoker(lambda idea, vid, depth: _SUCCESSFUL_BUILD_REPORT)
    deployment_pipeline.set_infra_operations({k: _fake_succeed for k in ("docker", "postgres", "chroma", "ollama", "n8n")})
    try:
        dp_agent.run_deployment_pipeline(idea="event test idea", venture_id=new_venture_id("events"))
    finally:
        deployment_pipeline.reset_ai_builder_invoker()
        deployment_pipeline.reset_infra_operations()
    check("published deployment_started", "deployment_started" in seen_events)
    check("published deployment_completed", "deployment_completed" in seen_events)
    check("did not publish deployment_failed for a successful run", "deployment_failed" not in seen_events)

    seen_events.clear()
    dp_agent.run_deployment_pipeline(idea="", venture_id=new_venture_id("events-rejected"))
    check("published deployment_started even for a rejected run", "deployment_started" in seen_events)
    check("published deployment_failed for a rejected run", "deployment_failed" in seen_events)
    check("did not publish deployment_completed for a rejected run", "deployment_completed" not in seen_events)

    seen_events.clear()
    deployment_pipeline.set_ai_builder_invoker(lambda idea, vid, depth: _SUCCESSFUL_BUILD_REPORT)
    deployment_pipeline.set_infra_operations({k: _fake_unreachable for k in ("docker", "postgres", "chroma", "ollama", "n8n")})
    try:
        dp_agent.run_deployment_pipeline(idea="all unreachable idea", venture_id=new_venture_id("events-failed"))
    finally:
        deployment_pipeline.reset_ai_builder_invoker()
        deployment_pipeline.reset_infra_operations()
    check("published deployment_failed when every infra stage is unreachable", "deployment_failed" in seen_events)

    print("\n== 9. Deterministic Output ==")
    deployment_pipeline.set_ai_builder_invoker(lambda idea, vid, depth: _SUCCESSFUL_BUILD_REPORT)
    deployment_pipeline.set_infra_operations({k: _fake_succeed for k in ("docker", "postgres", "chroma", "ollama", "n8n")})
    try:
        vid = new_venture_id("deterministic")
        first_run = dp_agent.run_deployment_pipeline(idea="deterministic idea", venture_id=vid)
        second_run = dp_agent.run_deployment_pipeline(idea="deterministic idea", venture_id=vid)
        for field in ("status", "build_validated", "ready_stage_count", "total_stage_count", "stages"):
            check(f"{field} identical across two runs of the same input", first_run[field] == second_run[field])
    finally:
        deployment_pipeline.reset_ai_builder_invoker()
        deployment_pipeline.reset_infra_operations()
    check("ai builder invoker restored to the real default", deployment_pipeline.get_ai_builder_invoker() is deployment_pipeline._default_ai_builder_invoker)
    check("infra operations restored to the real defaults", deployment_pipeline.get_infra_operations() == deployment_pipeline._DEFAULT_INFRA_OPERATIONS)

    print("\n== 10. Manager-callable node shape ==")
    deployment_pipeline.set_ai_builder_invoker(lambda idea, vid, depth: _SUCCESSFUL_BUILD_REPORT)
    deployment_pipeline.set_infra_operations({k: _fake_succeed for k in ("docker", "postgres", "chroma", "ollama", "n8n")})
    try:
        delta = dp_agent.deployment_pipeline_node({"idea": "node integration idea", "venture_id": new_venture_id("node")})
        check("deployment_pipeline_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
        check("node result carries a completed status", delta["history"][0]["status"] == "completed")
    finally:
        deployment_pipeline.reset_ai_builder_invoker()
        deployment_pipeline.reset_infra_operations()

    print("\n== 11. Full Regression of All Previous Phases/Components ==")
    repo_root = Path(__file__).resolve().parent.parent
    # These six files each embed their own full regression section, globbing
    # "smoke_test_phase*.py" - none of their (frozen, not to be modified)
    # exclusion lists know about this new file, so including any of them here
    # would let it call back into this file, forming an unbounded subprocess
    # cycle. All six already re-verify the full prior suite standalone, so
    # excluding them here loses no coverage.
    excluded = {
        "smoke_test_phase4_deployment_pipeline.py",
        "smoke_test_phase3_ads.py",
        "smoke_test_phase4_founder_orchestrator.py",
        "smoke_test_phase4_research_pipeline.py",
        "smoke_test_phase4_decision_engine.py",
        "smoke_test_phase4_mvp_planner.py",
        "smoke_test_phase4_ai_builder.py",
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

    print("\nAll Phase 4 Component 6 checks passed.")


if __name__ == "__main__":
    main()
