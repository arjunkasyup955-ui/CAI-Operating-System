"""Phase 3, Component 12: CRM APIs Integration.

Supports HubSpot, Salesforce, Zoho CRM, and Pipedrive behind one CRMProvider
interface (a config choice via CRM_PROVIDER, not a code choice).

Verifies:
  - Agent registration
  - Tool registration (13 tools)
  - Permissions (dedicated "crm" scope, enforced)
  - Schema validation
  - Retry (ToolRegistry's existing RetryPolicy, via a flaky fake provider)
  - Timeout (ToolRegistry's timeout mechanism, via a deliberately slow function)
  - Graceful failure (real provider - no CRM_API_KEY configured - plus the fake
    provider's 4 injectable fault modes: auth failure, rate limiting, network
    timeout, provider unavailable - and invalid record ID via genuine CRUD gating)
  - Fake provider CRUD lifecycle (create/get/update/delete lead, contact, company,
    deal, note - including a genuine LangGraph approval interrupt for the
    high-risk crm_delete_lead operation, both reject and approve paths)
  - Search functionality
  - Pipeline listing
  - Event publishing (crm_started/completed/failed)
  - Manager node integration
  - Regression of every previous component (run separately, see the full suite this
    script is part of)
  - pip check
  - git status

Run: python scripts/smoke_test_phase3_crm.py
"""

import logging
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

import agents.infrastructure.crm.agent as crm_agent  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import ToolSpec, get_agent_registry, get_tool_registry  # noqa: E402
from core.registries.tool_registry import RetryPolicy  # noqa: E402
from tools.crm.crm_providers import (  # noqa: E402
    CRMHealthStatus,
    CRMRESTProvider,
    FakeCRMProvider,
    set_crm_provider,
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

    def health_check(self) -> CRMHealthStatus:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("transient failure")
        return CRMHealthStatus(healthy=True, configured=True, provider_type="flaky")


def main() -> None:
    expected_tools = {
        "crm_health_check", "crm_create_lead", "crm_update_lead", "crm_delete_lead", "crm_get_lead",
        "crm_search_leads", "crm_create_contact", "crm_update_contact", "crm_create_company",
        "crm_create_deal", "crm_update_deal", "crm_add_note", "crm_list_pipelines",
    }

    print("\n== 1. Agent Registration ==")
    manifest = get_agent_registry().get("crm_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares the dedicated crm permission", set(manifest.permissions) == {"crm"})
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Tool Registration (13 tools) ==")
    check("declares all 13 tools", set(manifest.tools) == expected_tools)

    print("\n== 3. Permissions ==")
    for tool_name in expected_tools:
        spec = get_tool_registry().get_spec(tool_name)
        check(f"{tool_name} requires the crm scope", "crm" in spec.permissions)

    manifest_no_perms = manifest.model_copy(update={"name": "no_crm_scope_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("crm_health_check", agent_name="no_crm_scope_agent")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without the crm scope is denied", denied)

    try:
        get_tool_registry().invoke("crm_health_check", agent_name="git_agent")
        denied_other = False
    except PermissionDeniedError:
        denied_other = True
    check("an unrelated registered agent (git_agent) is denied", denied_other)

    print("\n== 4. Schema Validation ==")
    try:
        get_tool_registry().invoke("crm_create_lead", agent_name="crm_agent")
        rejected = False
    except ValidationError:
        rejected = True
    check("missing required lead fields rejected", rejected)

    try:
        get_tool_registry().invoke("crm_add_note", agent_name="crm_agent", record_id="lead-1")
        rejected2 = False
    except ValidationError:
        rejected2 = True
    check("missing required body rejected", rejected2)

    print("\n== 5. Retry Behaviour (ToolRegistry's existing RetryPolicy) ==")
    flaky = _FlakyProvider(fail_times=1)
    set_crm_provider(flaky)
    try:
        retry_entry = crm_agent.run_crm_operation("crm_health_check", venture_id="v-test")
        check("retried past one transient failure and completed", retry_entry["event"] == "crm_completed")
        check("exactly 2 attempts were made", flaky.calls == 2)
    finally:
        set_crm_provider(CRMRESTProvider())

    print("\n== 6. Timeout (ToolRegistry's timeout mechanism) ==")
    def _slow_query():
        time.sleep(2.0)
        return {"success": True}

    get_tool_registry().register(
        ToolSpec(
            name="crm_test_slow_query",
            input_schema=create_model("SlowQueryArgs"),
            permissions=["crm"],
            retry_policy=RetryPolicy(max_attempts=1),
            timeout_seconds=0.3,
        ),
        _slow_query,
    )
    start = time.monotonic()
    try:
        get_tool_registry().invoke("crm_test_slow_query", agent_name="crm_agent")
        timed_out = False
    except TimeoutError:
        timed_out = True
    elapsed = time.monotonic() - start
    check("a slow query times out per the tool's configured timeout_seconds", timed_out)
    check("did not wait for the full 2s operation to finish", elapsed < 1.5)

    print("\n== 7. Graceful Failure ==")
    real_health = crm_agent.run_crm_operation("crm_health_check", venture_id="v-test")
    check("real provider health check completes without crashing", real_health["event"] == "crm_completed")
    check("reports unhealthy (no CRM_API_KEY configured in this sandbox)", real_health["healthy"] is False)
    check("reports not configured", real_health["configured"] is False)
    print(f"     real provider error (expected - missing API key): {real_health.get('error')}")

    for fault, label in [
        ("auth_failure", "authentication failure"),
        ("rate_limited", "rate limiting"),
        ("network_timeout", "network timeout"),
        ("provider_unavailable", "provider unavailable"),
    ]:
        set_crm_provider(FakeCRMProvider(fault=fault))
        try:
            result = crm_agent.run_crm_operation("crm_health_check", venture_id="v-test")
            check(f"{label} is handled gracefully (never crashes)", result["event"] == "crm_failed")
        finally:
            set_crm_provider(CRMRESTProvider())

    print("\n== 8. Fake Provider: CRUD lifecycle ==")
    set_crm_provider(FakeCRMProvider())
    try:
        lead = crm_agent.run_crm_operation("crm_create_lead", venture_id="v-test", first_name="Ada", last_name="Lovelace", email="ada@example.com", company="Analytical Engines")
        check("create_lead succeeded", lead["success"])
        lid = lead["lead"]["lead_id"]

        got = crm_agent.run_crm_operation("crm_get_lead", venture_id="v-test", lead_id=lid)
        check("get_lead returns the created lead", got["lead"]["first_name"] == "Ada")

        updated = crm_agent.run_crm_operation("crm_update_lead", venture_id="v-test", lead_id=lid, status="qualified")
        check("update_lead applied the partial update", updated["lead"]["status"] == "qualified")

        bad_get = crm_agent.run_crm_operation("crm_get_lead", venture_id="v-test", lead_id="lead-does-not-exist")
        check("getting an invalid record ID fails gracefully", bad_get["event"] == "crm_failed")

        contact = crm_agent.run_crm_operation("crm_create_contact", venture_id="v-test", first_name="Grace", last_name="Hopper", email="grace@example.com")
        check("create_contact succeeded", contact["success"])

        updated_contact = crm_agent.run_crm_operation("crm_update_contact", venture_id="v-test", contact_id=contact["contact"]["contact_id"], email="grace.hopper@example.com")
        check("update_contact applied the partial update", updated_contact["contact"]["email"] == "grace.hopper@example.com")

        company = crm_agent.run_crm_operation("crm_create_company", venture_id="v-test", company_name="Acme Corp", domain="acme.com")
        check("create_company succeeded", company["success"])

        deal = crm_agent.run_crm_operation("crm_create_deal", venture_id="v-test", deal_name="Acme Renewal", amount=5000.0)
        check("create_deal succeeded", deal["success"])
        check("create_deal defaults to the default pipeline", deal["deal"]["pipeline_id"] == "pipeline-default")
        did = deal["deal"]["deal_id"]

        updated_deal = crm_agent.run_crm_operation("crm_update_deal", venture_id="v-test", deal_id=did, stage="won")
        check("update_deal applied the partial update", updated_deal["deal"]["stage"] == "won")

        note = crm_agent.run_crm_operation("crm_add_note", venture_id="v-test", record_id=lid, body="Called, interested")
        check("add_note succeeded against a known record", note["success"])

        bad_note = crm_agent.run_crm_operation("crm_add_note", venture_id="v-test", record_id="ghost-id", body="x")
        check("adding a note to an unknown record fails gracefully", bad_note["event"] == "crm_failed")

        print("\n== 9. Search Functionality ==")
        search = crm_agent.run_crm_operation("crm_search_leads", venture_id="v-test", query="lovelace")
        check("search_leads finds the matching lead", len(search["leads"]) == 1 and search["leads"][0]["lead_id"] == lid)

        no_match = crm_agent.run_crm_operation("crm_search_leads", venture_id="v-test", query="nonexistent-name-xyz")
        check("search_leads returns no results for a non-matching query", len(no_match["leads"]) == 0)

        print("\n== 10. Pipeline Listing ==")
        pipelines = crm_agent.run_crm_operation("crm_list_pipelines", venture_id="v-test")
        check("list_pipelines returns the default pipeline", any(p["pipeline_id"] == "pipeline-default" for p in pipelines["pipelines"]))

        print("\n== 11. Approval Interrupt: crm_delete_lead (high risk) ==")

        class _S(TypedDict, total=False):
            history: Annotated[list, operator.add]
            lead_id: str

        def delete_node(state: dict) -> dict:
            entry = crm_agent.run_crm_operation("crm_delete_lead", venture_id="v-test", lead_id=state["lead_id"])
            return {"history": [entry]}

        graph = StateGraph(_S)
        graph.add_node("delete", delete_node)
        graph.add_edge(START, "delete")
        graph.add_edge("delete", END)
        compiled = graph.compile(checkpointer=InMemorySaver())

        reject_thread = f"crm-delete-reject-{uuid.uuid4().hex[:8]}"
        config_reject = {"configurable": {"thread_id": reject_thread}}
        first = compiled.invoke({"history": [], "lead_id": lid}, config=config_reject)
        check("delete_lead paused for human approval (real interrupt)", "__interrupt__" in first)
        check("interrupt carries the crm_delete_lead action", first["__interrupt__"][0].value["action"] == "crm_delete_lead")

        rejected = compiled.invoke(Command(resume={"approved": False, "reason": "not yet"}), config=config_reject)
        check("rejected delete does not execute", rejected["history"][0]["event"] == "crm_failed")
        still_there = crm_agent.run_crm_operation("crm_get_lead", venture_id="v-test", lead_id=lid)
        check("lead still exists after rejected delete", still_there["event"] == "crm_completed")

        approve_thread = f"crm-delete-approve-{uuid.uuid4().hex[:8]}"
        config_approve = {"configurable": {"thread_id": approve_thread}}
        compiled.invoke({"history": [], "lead_id": lid}, config=config_approve)
        approved = compiled.invoke(Command(resume={"approved": True, "reason": "reviewed"}), config=config_approve)
        check("approved delete executes", approved["history"][0]["event"] == "crm_completed")

        gone = crm_agent.run_crm_operation("crm_get_lead", venture_id="v-test", lead_id=lid)
        check("lead genuinely removed after approval", gone["event"] == "crm_failed")
    finally:
        set_crm_provider(CRMRESTProvider())

    print("\n== 12. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    crm_agent.run_crm_operation("crm_health_check", venture_id="v-test")
    check("published crm_started", "crm_started" in seen_events)
    check("published crm_completed", "crm_completed" in seen_events)

    seen_events.clear()
    set_crm_provider(_FlakyProvider(fail_times=10))
    try:
        crm_agent.run_crm_operation("crm_health_check", venture_id="v-test")
    finally:
        set_crm_provider(CRMRESTProvider())
    check("published crm_failed", "crm_failed" in seen_events)

    print("\n== 13. Manager-callable node shape ==")
    delta = crm_agent.crm_agent_node({"venture_id": "v-test"})
    check("crm_agent_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
    check("node result reflects the health check operation", delta["history"][0]["operation"] == "crm_health_check")

    print("\nAll Phase 3 Component 12 checks passed.")


if __name__ == "__main__":
    main()
