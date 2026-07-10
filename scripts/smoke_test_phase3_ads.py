"""Phase 3, Component 13: Ad APIs Integration.

Supports Google Ads, Meta Ads, LinkedIn Ads, and X Ads behind one AdsProvider
interface (a config choice via ADS_PROVIDER, not a code choice).

Verifies:
  - Agent registration
  - Tool registration (15 tools)
  - Permissions (dedicated "advertisement" scope, enforced)
  - Schema validation
  - Retry (ToolRegistry's existing RetryPolicy, via a flaky fake provider)
  - Timeout (ToolRegistry's timeout mechanism, via a deliberately slow function)
  - Graceful failure (real provider - no ADS_API_KEY configured - plus the fake
    provider's 4 injectable fault modes: auth failure, rate limiting, network
    timeout, provider unavailable - and invalid campaign ID via genuine CRUD
    gating)
  - Fake provider campaign lifecycle (create/get/update/pause/resume/delete,
    including idempotency gating on pause/resume and a genuine LangGraph approval
    interrupt for the high-risk ads_delete_campaign operation)
  - Analytics
  - Budget updates (including rejection of a non-positive budget)
  - Audience lookup
  - Keyword suggestions
  - Event publishing (ads_started/completed/failed)
  - Manager node integration
  - Regression of every previous component (run separately, see the full suite this
    script is part of)
  - pip check
  - git status

Run: python scripts/smoke_test_phase3_ads.py
"""

import logging
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Annotated, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

import operator  # noqa: E402

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command  # noqa: E402
from pydantic import ValidationError, create_model  # noqa: E402

import agents.infrastructure.ads.agent as ads_agent  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import ToolSpec, get_agent_registry, get_tool_registry  # noqa: E402
from core.registries.tool_registry import RetryPolicy  # noqa: E402
from tools.ads.ads_providers import (  # noqa: E402
    AdsHealthStatus,
    AdsRESTProvider,
    FakeAdsProvider,
    set_ads_provider,
)


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


class _FlakyProvider:
    name = "flaky"

    def __init__(self, fail_times: int = 1) -> None:
        self.calls = 0
        self._fail_times = fail_times

    def health_check(self) -> AdsHealthStatus:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("transient failure")
        return AdsHealthStatus(healthy=True, configured=True, provider_type="flaky")


def main() -> None:
    expected_tools = {
        "ads_health_check", "ads_create_campaign", "ads_update_campaign", "ads_pause_campaign",
        "ads_resume_campaign", "ads_delete_campaign", "ads_get_campaign", "ads_list_campaigns",
        "ads_campaign_analytics", "ads_budget_update", "ads_audience_lookup", "ads_keyword_suggestions",
        "ads_ad_preview", "ads_create_ad_group", "ads_create_ad",
    }

    print("\n== 1. Agent Registration ==")
    manifest = get_agent_registry().get("ads_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares the dedicated advertisement scope", set(manifest.permissions) == {"advertisement"})
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Tool Registration (15 tools) ==")
    check("declares all 15 tools", set(manifest.tools) == expected_tools)

    print("\n== 3. Permissions ==")
    for tool_name in expected_tools:
        spec = get_tool_registry().get_spec(tool_name)
        check(f"{tool_name} requires the advertisement scope", "advertisement" in spec.permissions)

    manifest_no_perms = manifest.model_copy(update={"name": "no_ads_scope_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("ads_health_check", agent_name="no_ads_scope_agent")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without the advertisement scope is denied", denied)

    try:
        get_tool_registry().invoke("ads_health_check", agent_name="git_agent")
        denied_other = False
    except PermissionDeniedError:
        denied_other = True
    check("an unrelated registered agent (git_agent) is denied", denied_other)

    print("\n== 4. Schema Validation ==")
    try:
        get_tool_registry().invoke("ads_create_campaign", agent_name="ads_agent")
        rejected = False
    except ValidationError:
        rejected = True
    check("missing required campaign_name rejected", rejected)

    try:
        get_tool_registry().invoke("ads_budget_update", agent_name="ads_agent", campaign_id="campaign-1")
        rejected2 = False
    except ValidationError:
        rejected2 = True
    check("missing required budget rejected", rejected2)

    print("\n== 5. Retry Behaviour (ToolRegistry's existing RetryPolicy) ==")
    flaky = _FlakyProvider(fail_times=1)
    set_ads_provider(flaky)
    try:
        retry_entry = ads_agent.run_ads_operation("ads_health_check", venture_id="v-test")
        check("retried past one transient failure and completed", retry_entry["event"] == "ads_completed")
        check("exactly 2 attempts were made", flaky.calls == 2)
    finally:
        set_ads_provider(AdsRESTProvider())

    print("\n== 6. Timeout (ToolRegistry's timeout mechanism) ==")
    def _slow_query():
        time.sleep(2.0)
        return {"success": True}

    get_tool_registry().register(
        ToolSpec(
            name="ads_test_slow_query",
            input_schema=create_model("SlowQueryArgs"),
            permissions=["advertisement"],
            retry_policy=RetryPolicy(max_attempts=1),
            timeout_seconds=0.3,
        ),
        _slow_query,
    )
    start = time.monotonic()
    try:
        get_tool_registry().invoke("ads_test_slow_query", agent_name="ads_agent")
        timed_out = False
    except TimeoutError:
        timed_out = True
    elapsed = time.monotonic() - start
    check("a slow query times out per the tool's configured timeout_seconds", timed_out)
    check("did not wait for the full 2s operation to finish", elapsed < 1.5)

    print("\n== 7. Graceful Failure ==")
    real_health = ads_agent.run_ads_operation("ads_health_check", venture_id="v-test")
    check("real provider health check completes without crashing", real_health["event"] == "ads_completed")
    check("reports unhealthy (no ADS_API_KEY configured in this sandbox)", real_health["healthy"] is False)
    check("reports not configured", real_health["configured"] is False)
    print(f"     real provider error (expected - missing API key): {real_health.get('error')}")

    for fault, label in [
        ("auth_failure", "authentication failure"),
        ("rate_limited", "rate limiting"),
        ("network_timeout", "network timeout"),
        ("provider_unavailable", "provider unavailable"),
    ]:
        set_ads_provider(FakeAdsProvider(fault=fault))
        try:
            result = ads_agent.run_ads_operation("ads_health_check", venture_id="v-test")
            check(f"{label} is handled gracefully (never crashes)", result["event"] == "ads_failed")
        finally:
            set_ads_provider(AdsRESTProvider())

    print("\n== 8. Fake Provider: campaign lifecycle ==")
    set_ads_provider(FakeAdsProvider())
    try:
        campaign = ads_agent.run_ads_operation("ads_create_campaign", venture_id="v-test", campaign_name="Launch Promo", budget=100.0)
        check("create_campaign succeeded", campaign["success"])
        check("new campaign starts active", campaign["campaign"]["status"] == "active")
        cid = campaign["campaign"]["campaign_id"]

        updated = ads_agent.run_ads_operation("ads_update_campaign", venture_id="v-test", campaign_id=cid, objective="awareness")
        check("update_campaign applied the partial update", updated["campaign"]["objective"] == "awareness")

        paused = ads_agent.run_ads_operation("ads_pause_campaign", venture_id="v-test", campaign_id=cid)
        check("pause_campaign succeeded", paused["campaign"]["status"] == "paused")

        double_pause = ads_agent.run_ads_operation("ads_pause_campaign", venture_id="v-test", campaign_id=cid)
        check("pausing an already-paused campaign fails gracefully", double_pause["event"] == "ads_failed")

        resumed = ads_agent.run_ads_operation("ads_resume_campaign", venture_id="v-test", campaign_id=cid)
        check("resume_campaign succeeded", resumed["campaign"]["status"] == "active")

        bad_get = ads_agent.run_ads_operation("ads_get_campaign", venture_id="v-test", campaign_id="campaign-does-not-exist")
        check("getting an invalid campaign ID fails gracefully", bad_get["event"] == "ads_failed")

        listed = ads_agent.run_ads_operation("ads_list_campaigns", venture_id="v-test")
        check("list_campaigns returns the created campaign", len(listed["campaigns"]) == 1)

        print("\n== 9. Analytics ==")
        analytics = ads_agent.run_ads_operation("ads_campaign_analytics", venture_id="v-test", campaign_id=cid)
        check("campaign_analytics succeeded", analytics["success"])
        check("campaign_analytics returns non-zero impressions", analytics["analytics"]["impressions"] > 0)

        bad_analytics = ads_agent.run_ads_operation("ads_campaign_analytics", venture_id="v-test", campaign_id="campaign-does-not-exist")
        check("analytics for an invalid campaign ID fails gracefully", bad_analytics["event"] == "ads_failed")

        print("\n== 10. Budget Updates ==")
        budget = ads_agent.run_ads_operation("ads_budget_update", venture_id="v-test", campaign_id=cid, budget=250.0)
        check("budget_update applied the new budget", budget["campaign"]["budget"] == 250.0)

        bad_budget = ads_agent.run_ads_operation("ads_budget_update", venture_id="v-test", campaign_id=cid, budget=-5.0)
        check("a non-positive budget is rejected gracefully", bad_budget["event"] == "ads_failed")

        print("\n== 11. Audience Lookup ==")
        audiences = ads_agent.run_ads_operation("ads_audience_lookup", venture_id="v-test", query="founders")
        check("audience_lookup finds a matching segment", len(audiences["audiences"]) == 1 and "founders" in audiences["audiences"][0]["segment_name"].lower())

        no_match_audience = ads_agent.run_ads_operation("ads_audience_lookup", venture_id="v-test", query="nonexistent-segment-xyz")
        check("audience_lookup returns no results for a non-matching query", len(no_match_audience["audiences"]) == 0)

        print("\n== 12. Keyword Suggestions ==")
        keywords = ads_agent.run_ads_operation("ads_keyword_suggestions", venture_id="v-test", seed_keyword="saas tools", max_results=3)
        check("keyword_suggestions returns the requested count", len(keywords["keywords"]) == 3)
        check("keyword_suggestions incorporates the seed keyword", all("saas tools" in k["keyword"] for k in keywords["keywords"]))

        ad_group = ads_agent.run_ads_operation("ads_create_ad_group", venture_id="v-test", campaign_id=cid, ad_group_name="Group A")
        check("create_ad_group succeeded", ad_group["success"])
        agid = ad_group["ad_group"]["ad_group_id"]

        bad_ad_group = ads_agent.run_ads_operation("ads_create_ad_group", venture_id="v-test", campaign_id="campaign-does-not-exist", ad_group_name="X")
        check("creating an ad group under an invalid campaign fails gracefully", bad_ad_group["event"] == "ads_failed")

        ad = ads_agent.run_ads_operation("ads_create_ad", venture_id="v-test", ad_group_id=agid, headline="Buy now", body="Great deal")
        check("create_ad succeeded", ad["success"])
        aid = ad["ad"]["ad_id"]

        preview = ads_agent.run_ads_operation("ads_ad_preview", venture_id="v-test", ad_id=aid)
        check("ad_preview reflects the ad's headline/body", "Buy now" in preview["preview"]["preview_text"] and "Great deal" in preview["preview"]["preview_text"])

        print("\n== 13. Approval Interrupt: ads_delete_campaign (high risk) ==")

        class _S(TypedDict, total=False):
            history: Annotated[list, operator.add]
            campaign_id: str

        def delete_node(state: dict) -> dict:
            entry = ads_agent.run_ads_operation("ads_delete_campaign", venture_id="v-test", campaign_id=state["campaign_id"])
            return {"history": [entry]}

        graph = StateGraph(_S)
        graph.add_node("delete", delete_node)
        graph.add_edge(START, "delete")
        graph.add_edge("delete", END)
        compiled = graph.compile(checkpointer=InMemorySaver())

        reject_thread = f"ads-delete-reject-{uuid.uuid4().hex[:8]}"
        config_reject = {"configurable": {"thread_id": reject_thread}}
        first = compiled.invoke({"history": [], "campaign_id": cid}, config=config_reject)
        check("delete_campaign paused for human approval (real interrupt)", "__interrupt__" in first)
        check("interrupt carries the ads_delete_campaign action", first["__interrupt__"][0].value["action"] == "ads_delete_campaign")

        rejected = compiled.invoke(Command(resume={"approved": False, "reason": "not yet"}), config=config_reject)
        check("rejected delete does not execute", rejected["history"][0]["event"] == "ads_failed")
        still_there = ads_agent.run_ads_operation("ads_get_campaign", venture_id="v-test", campaign_id=cid)
        check("campaign still exists after rejected delete", still_there["event"] == "ads_completed")

        approve_thread = f"ads-delete-approve-{uuid.uuid4().hex[:8]}"
        config_approve = {"configurable": {"thread_id": approve_thread}}
        compiled.invoke({"history": [], "campaign_id": cid}, config=config_approve)
        approved = compiled.invoke(Command(resume={"approved": True, "reason": "reviewed"}), config=config_approve)
        check("approved delete executes", approved["history"][0]["event"] == "ads_completed")

        gone = ads_agent.run_ads_operation("ads_get_campaign", venture_id="v-test", campaign_id=cid)
        check("campaign genuinely removed after approval", gone["event"] == "ads_failed")
    finally:
        set_ads_provider(AdsRESTProvider())

    print("\n== 14. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    ads_agent.run_ads_operation("ads_health_check", venture_id="v-test")
    check("published ads_started", "ads_started" in seen_events)
    check("published ads_completed", "ads_completed" in seen_events)

    seen_events.clear()
    set_ads_provider(_FlakyProvider(fail_times=10))
    try:
        ads_agent.run_ads_operation("ads_health_check", venture_id="v-test")
    finally:
        set_ads_provider(AdsRESTProvider())
    check("published ads_failed", "ads_failed" in seen_events)

    print("\n== 15. Manager-callable node shape ==")
    delta = ads_agent.ads_agent_node({"venture_id": "v-test"})
    check("ads_agent_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
    check("node result reflects the health check operation", delta["history"][0]["operation"] == "ads_health_check")

    print("\n== 16. Full Regression of All Previous Phases/Components ==")
    repo_root = Path(__file__).resolve().parent.parent
    smoke_tests = sorted(
        p for p in (repo_root / "scripts").glob("smoke_test_phase*.py")
        if p.name != "smoke_test_phase3_ads.py"
    )
    for test_path in smoke_tests:
        result = subprocess.run([sys.executable, str(test_path)], cwd=repo_root, capture_output=True, text=True)
        check(f"regression: {test_path.name}", result.returncode == 0)

    print("\n== 17. pip check ==")
    pip_result = subprocess.run([sys.executable, "-m", "pip", "check"], cwd=repo_root, capture_output=True, text=True)
    check("pip check reports no broken requirements", pip_result.returncode == 0)

    print("\n== 18. git status (only new files, nothing modified) ==")
    git_result = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True)
    status_lines = [line for line in git_result.stdout.splitlines() if line.strip()]
    modified_or_deleted = [line for line in status_lines if not line.startswith("??")]
    check("git status has no modified/deleted files, only untracked additions", len(modified_or_deleted) == 0)

    print("\nAll Phase 3 Component 13 checks passed.")


if __name__ == "__main__":
    main()
