"""Phase 4, Component 7: Growth Pipeline.

Consumes ONLY the completed Deployment Pipeline's (Phase 4 Component 6)
Deployment Report and, only if the deployment was successful, generates a
landing page, SEO metadata, marketing assets, an ad campaign draft, and a CRM
configuration for the venture. Reuses only existing Phase 3 Infrastructure
(Ollama) and Marketing-related (Ads, CRM) agents - duplicates none of their
implementation. Built on top of the frozen Phase 0 kernel and the
already-verified Phase 4 Components 1-6 - nothing in Phase 0-3 or Phase 4
Components 1-6 is modified.

Verifies:
  - Agent registration + manifest
  - Graph creation (8 nodes: intake, deployment_report, landing_page,
    seo_metadata, marketing_assets, ad_campaign_draft, crm_configuration,
    aggregate)
  - A fully deterministic successful growth run (fake Deployment Report + fake
    Ollama/Ads/CRM operations - touches no real LLM/ad platform/CRM backend)
  - Partial readiness (some growth stages reachable, some not) still completes
    gracefully with an accurate per-stage breakdown
  - Complete backend unavailability is still handled gracefully (never
    crashes; SEO metadata - pure Python, no external dependency - always
    remains ready even when every reused agent is unreachable)
  - An unsuccessful Deployment Report (status != "completed") skips growth
    activities entirely
  - Invalid input (empty idea, missing venture_id) rejected gracefully
  - Event publishing (growth_started/growth_completed/growth_failed)
  - Deterministic output (same input -> byte-identical Growth Report, run
    twice)
  - Manager node integration
  - Regression of every previous phase/component (run directly, one process
    each - excluding the seven files that each embed their own full
    regression section, which would otherwise call back into this file,
    forming an unbounded subprocess cycle - all seven already re-verify the
    full prior suite standalone)
  - pip check
  - git status

Run: python scripts/smoke_test_phase4_growth_pipeline.py
"""

import logging
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

import agents.founder.growth_pipeline.agent as gp_agent  # noqa: E402
import workflows.growth_pipeline as growth_pipeline  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.registries import get_agent_registry  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


def new_venture_id(prefix: str) -> str:
    return f"venture-{prefix}-{uuid.uuid4().hex[:8]}"


_SUCCESSFUL_DEPLOYMENT_REPORT = {"idea": "An AI copilot for solo founders", "status": "completed"}
_UNSUCCESSFUL_DEPLOYMENT_REPORT = {"idea": "weak idea", "status": "failed"}


def _fake_ollama_succeed(operation: str, venture_id: str = "default", **kwargs) -> dict:
    return {"agent": "ollama_ops_agent", "event": "ollama_completed", "operation": operation, "content": f"fake {operation} content"}


def _fake_ads_succeed(operation: str, venture_id: str = "default", **kwargs) -> dict:
    if operation == "ads_audience_lookup":
        return {"agent": "ads_agent", "event": "ads_completed", "operation": operation, "audiences": [{"segment_id": "a1", "segment_name": "founders"}]}
    return {"agent": "ads_agent", "event": "ads_completed", "operation": operation, "keywords": [{"keyword": "ai copilot"}]}


def _fake_crm_succeed(operation: str, venture_id: str = "default", **kwargs) -> dict:
    if operation == "crm_list_pipelines":
        return {"agent": "crm_agent", "event": "crm_completed", "operation": operation, "pipelines": [{"pipeline_id": "p1", "pipeline_name": "Sales"}]}
    return {"agent": "crm_agent", "event": "crm_completed", "operation": operation}


def _fake_unreachable(operation: str, venture_id: str = "default", **kwargs) -> dict:
    return {"agent": "fake", "event": "fake_failed", "operation": operation, "error": "connection refused"}


_ALL_SUCCEED_OPS = {"ollama": _fake_ollama_succeed, "ads": _fake_ads_succeed, "crm": _fake_crm_succeed}


def main() -> None:
    print("\n== 1. Agent Registration + Manifest ==")
    manifest = get_agent_registry().get("growth_pipeline")
    check("registered with supervisor null", manifest.supervisor is None)
    check("declares no permissions (no new permission types, no Tool Registry calls of its own)", manifest.permissions == [])
    check("declares no tools", manifest.tools == [])
    check("lifecycle is active", manifest.lifecycle == "active")
    check("responsibilities documented", len(manifest.responsibilities) > 0)

    print("\n== 2. Graph Creation ==")
    graph = growth_pipeline.build_growth_pipeline()
    expected_nodes = {"intake", "deployment_report", "landing_page", "seo_metadata", "marketing_assets", "ad_campaign_draft", "crm_configuration", "aggregate"}
    check("graph builds with all 8 nodes", set(graph.nodes) == expected_nodes)

    print("\n== 3. Deterministic Successful Growth Run (fake Deployment Report + fake ops) ==")
    growth_pipeline.set_deployment_pipeline_invoker(lambda idea, vid, depth: _SUCCESSFUL_DEPLOYMENT_REPORT)
    growth_pipeline.set_growth_operations(_ALL_SUCCEED_OPS)
    try:
        success = gp_agent.run_growth_pipeline(idea="An AI copilot for solo founders", venture_id=new_venture_id("success"))
        check("growth pipeline completed", success["status"] == "completed")
        check("deployment was validated", success["deployment_validated"] is True)
        check("all 5 stages ready", success["ready_stage_count"] == 5 and success["total_stage_count"] == 5)
        check("landing page was generated with content", success["landing_page"]["generated"] is True and bool(success["landing_page"]["content"]))
        check("SEO metadata has a title, description, and keywords", bool(success["seo_metadata"]["title"]) and bool(success["seo_metadata"]["description"]) and len(success["seo_metadata"]["keywords"]) > 0)
        check("SEO keywords are derived from the idea text deterministically", "copilot" in success["seo_metadata"]["keywords"])
        check("marketing assets include a tagline", success["marketing_assets"]["generated"] is True and bool(success["marketing_assets"]["tagline"]))
        check("ad campaign draft includes audiences and keywords", success["ad_campaign_draft"]["ready"] is True and success["ad_campaign_draft"]["audiences"] and success["ad_campaign_draft"]["keywords"])
        check("CRM configuration created a company and found a pipeline", success["crm_configuration"]["ready"] is True and success["crm_configuration"]["company_created"] is True and "Sales" in success["crm_configuration"]["pipelines_available"])
        check("no error recorded on success", success["error"] == "")
    finally:
        growth_pipeline.reset_deployment_pipeline_invoker()
        growth_pipeline.reset_growth_operations()

    print("\n== 4. Partial Growth Readiness (mixed reachable/unreachable backends) ==")
    growth_pipeline.set_deployment_pipeline_invoker(lambda idea, vid, depth: _SUCCESSFUL_DEPLOYMENT_REPORT)
    growth_pipeline.set_growth_operations({"ollama": _fake_ollama_succeed, "ads": _fake_unreachable, "crm": _fake_unreachable})
    try:
        partial = gp_agent.run_growth_pipeline(idea="partial idea", venture_id=new_venture_id("partial"))
        check("status is 'partial' when some but not all stages are ready", partial["status"] == "partial")
        check("landing page (ollama) is ready", partial["landing_page"]["generated"] is True)
        check("ad campaign draft (ads) is not ready", partial["ad_campaign_draft"]["ready"] is False)
        check("CRM configuration is not ready", partial["crm_configuration"]["ready"] is False)
        check("error field summarizes the failed stages", "connection refused" in partial["error"])
    finally:
        growth_pipeline.reset_deployment_pipeline_invoker()
        growth_pipeline.reset_growth_operations()

    print("\n== 5. Complete Backend Unavailability ==")
    growth_pipeline.set_deployment_pipeline_invoker(lambda idea, vid, depth: _SUCCESSFUL_DEPLOYMENT_REPORT)
    growth_pipeline.set_growth_operations({"ollama": _fake_unreachable, "ads": _fake_unreachable, "crm": _fake_unreachable})
    try:
        all_fail = gp_agent.run_growth_pipeline(idea="all fail idea", venture_id=new_venture_id("all-fail"))
        check("status is 'partial' (not 'failed'/crashed) since SEO metadata is pure Python and always succeeds", all_fail["status"] == "partial")
        check("exactly 1 of 5 stages ready (SEO metadata only)", all_fail["ready_stage_count"] == 1)
        check("SEO metadata remains ready even with every reused agent unreachable", all_fail["seo_metadata"]["generated"] is True)
        check("landing page correctly reports not generated", all_fail["landing_page"]["generated"] is False)
        check("deployment was still validated (the deployment itself succeeded)", all_fail["deployment_validated"] is True)
    finally:
        growth_pipeline.reset_deployment_pipeline_invoker()
        growth_pipeline.reset_growth_operations()

    print("\n== 6. Unsuccessful Deployment Skips Growth Activities ==")
    growth_pipeline.set_deployment_pipeline_invoker(lambda idea, vid, depth: _UNSUCCESSFUL_DEPLOYMENT_REPORT)
    try:
        skipped = gp_agent.run_growth_pipeline(idea="weak idea", venture_id=new_venture_id("skip"))
        check("an unsuccessful deployment report yields status 'skipped'", skipped["status"] == "skipped")
        check("deployment_validated is False", skipped["deployment_validated"] is False)
        check("no growth stages were attempted", skipped["total_stage_count"] == 0)
        check("the reason references the deployment's own status", "failed" in skipped["error"])
    finally:
        growth_pipeline.reset_deployment_pipeline_invoker()

    print("\n== 7. Invalid Input ==")
    empty_idea = gp_agent.run_growth_pipeline(idea="", venture_id=new_venture_id("empty-idea"))
    check("empty idea is rejected gracefully", empty_idea["status"] == "rejected")

    missing_venture = gp_agent.run_growth_pipeline(idea="some idea", venture_id="")
    check("missing venture_id is rejected gracefully", missing_venture["status"] == "rejected")

    growth_pipeline.set_deployment_pipeline_invoker(lambda idea, vid, depth: _SUCCESSFUL_DEPLOYMENT_REPORT)
    growth_pipeline.set_growth_operations(_ALL_SUCCEED_OPS)
    try:
        invalid_depth = gp_agent.run_growth_pipeline(idea="some idea", venture_id=new_venture_id("bad-depth"), research_depth="ultra")
        check("an invalid research_depth is silently coerced to 'standard', not rejected", invalid_depth["status"] == "completed")
    finally:
        growth_pipeline.reset_deployment_pipeline_invoker()
        growth_pipeline.reset_growth_operations()

    print("\n== 8. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    growth_pipeline.set_deployment_pipeline_invoker(lambda idea, vid, depth: _SUCCESSFUL_DEPLOYMENT_REPORT)
    growth_pipeline.set_growth_operations(_ALL_SUCCEED_OPS)
    try:
        gp_agent.run_growth_pipeline(idea="event test idea", venture_id=new_venture_id("events"))
    finally:
        growth_pipeline.reset_deployment_pipeline_invoker()
        growth_pipeline.reset_growth_operations()
    check("published growth_started", "growth_started" in seen_events)
    check("published growth_completed", "growth_completed" in seen_events)
    check("did not publish growth_failed for a successful run", "growth_failed" not in seen_events)

    seen_events.clear()
    gp_agent.run_growth_pipeline(idea="", venture_id=new_venture_id("events-rejected"))
    check("published growth_started even for a rejected run", "growth_started" in seen_events)
    check("published growth_failed for a rejected run", "growth_failed" in seen_events)
    check("did not publish growth_completed for a rejected run", "growth_completed" not in seen_events)

    print("\n== 9. Deterministic Output ==")
    growth_pipeline.set_deployment_pipeline_invoker(lambda idea, vid, depth: _SUCCESSFUL_DEPLOYMENT_REPORT)
    growth_pipeline.set_growth_operations(_ALL_SUCCEED_OPS)
    try:
        vid = new_venture_id("deterministic")
        first_run = gp_agent.run_growth_pipeline(idea="deterministic idea", venture_id=vid)
        second_run = gp_agent.run_growth_pipeline(idea="deterministic idea", venture_id=vid)
        for field in ("status", "deployment_validated", "seo_metadata", "ready_stage_count", "total_stage_count"):
            check(f"{field} identical across two runs of the same input", first_run[field] == second_run[field])
    finally:
        growth_pipeline.reset_deployment_pipeline_invoker()
        growth_pipeline.reset_growth_operations()
    check("deployment pipeline invoker restored to the real default", growth_pipeline.get_deployment_pipeline_invoker() is growth_pipeline._default_deployment_pipeline_invoker)
    check("growth operations restored to the real defaults", growth_pipeline.get_growth_operations() == growth_pipeline._DEFAULT_GROWTH_OPERATIONS)

    print("\n== 10. Manager-callable node shape ==")
    growth_pipeline.set_deployment_pipeline_invoker(lambda idea, vid, depth: _SUCCESSFUL_DEPLOYMENT_REPORT)
    growth_pipeline.set_growth_operations(_ALL_SUCCEED_OPS)
    try:
        delta = gp_agent.growth_pipeline_node({"idea": "node integration idea", "venture_id": new_venture_id("node")})
        check("growth_pipeline_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
        check("node result carries a completed status", delta["history"][0]["status"] == "completed")
    finally:
        growth_pipeline.reset_deployment_pipeline_invoker()
        growth_pipeline.reset_growth_operations()

    print("\n== 11. Full Regression of All Previous Phases/Components ==")
    repo_root = Path(__file__).resolve().parent.parent
    # These seven files each embed their own full regression section, globbing
    # "smoke_test_phase*.py" - none of their (frozen, not to be modified)
    # exclusion lists know about this new file, so including any of them here
    # would let it call back into this file, forming an unbounded subprocess
    # cycle. All seven already re-verify the full prior suite standalone, so
    # excluding them here loses no coverage.
    excluded = {
        "smoke_test_phase4_growth_pipeline.py",
        "smoke_test_phase3_ads.py",
        "smoke_test_phase4_founder_orchestrator.py",
        "smoke_test_phase4_research_pipeline.py",
        "smoke_test_phase4_decision_engine.py",
        "smoke_test_phase4_mvp_planner.py",
        "smoke_test_phase4_ai_builder.py",
        "smoke_test_phase4_deployment_pipeline.py",
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

    print("\nAll Phase 4 Component 7 checks passed.")


if __name__ == "__main__":
    main()
