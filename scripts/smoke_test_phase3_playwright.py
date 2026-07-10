"""Phase 3, Component 6: Playwright Integration.

Verifies:
  - Registration (agent + 11 tools)
  - Permissions (internet_access, enforced)
  - Schema validation
  - Retry (ToolRegistry's existing RetryPolicy, via a flaky fake provider)
  - Timeout (ToolRegistry's timeout mechanism, via a deliberately slow function)
  - Graceful failure (real provider - playwright package not installed - never
    crashes)
  - Fake provider browser lifecycle (launch -> extract-before-open fails -> open_url
    -> extract_html/extract_text/screenshot -> fill_form -> click_element (success +
    failure) -> wait_for_selector (success + failure) -> close_browser -> use-after-
    close fails, deterministic, in-memory)
  - Approval interrupts (execute_javascript is high risk and genuinely pauses inside
    a real graph; both approve and reject paths)
  - Event publishing (playwright_started/completed/failed)
  - Regression of every previous component (run separately, see the full suite this
    script is part of)

Run: python scripts/smoke_test_phase3_playwright.py
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

import agents.infrastructure.playwright.agent as pw_agent  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import ToolSpec, get_agent_registry, get_tool_registry  # noqa: E402
from core.registries.tool_registry import RetryPolicy  # noqa: E402
from tools.playwright.playwright_providers import (  # noqa: E402
    FakePlaywrightProvider,
    PlaywrightHealthStatus,
    PlaywrightRealProvider,
    set_playwright_provider,
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

    def health_check(self) -> PlaywrightHealthStatus:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("transient failure")
        return PlaywrightHealthStatus(healthy=True, installed=True)


def main() -> None:
    expected_tools = {
        "playwright_health_check", "playwright_launch_browser", "playwright_close_browser",
        "playwright_open_url", "playwright_screenshot", "playwright_extract_html",
        "playwright_extract_text", "playwright_execute_javascript", "playwright_fill_form",
        "playwright_click_element", "playwright_wait_for_selector",
    }

    print("\n== 1. Registration (agent + 11 tools) ==")
    manifest = get_agent_registry().get("playwright_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares internet_access permission", set(manifest.permissions) == {"internet_access"})
    check("declares all 11 tools", set(manifest.tools) == expected_tools)
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Permissions ==")
    for tool_name in expected_tools:
        spec = get_tool_registry().get_spec(tool_name)
        check(f"{tool_name} requires internet_access", "internet_access" in spec.permissions)

    manifest_no_perms = manifest.model_copy(update={"name": "no_net_playwright_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("playwright_health_check", agent_name="no_net_playwright_agent")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without internet_access is denied", denied)

    try:
        get_tool_registry().invoke("playwright_health_check", agent_name="git_agent")
        denied_other = False
    except PermissionDeniedError:
        denied_other = True
    check("an unrelated registered agent (git_agent) is denied", denied_other)

    print("\n== 3. Schema Validation ==")
    try:
        get_tool_registry().invoke("playwright_open_url", agent_name="playwright_agent", browser_id="x")
        rejected = False
    except ValidationError:
        rejected = True
    check("missing required url rejected", rejected)

    print("\n== 4. Retry Behaviour (ToolRegistry's existing RetryPolicy) ==")
    flaky = _FlakyProvider(fail_times=1)
    set_playwright_provider(flaky)
    try:
        retry_entry = pw_agent.run_playwright_operation("playwright_health_check", venture_id="v-test")
        check("retried past one transient failure and completed", retry_entry["event"] == "playwright_completed")
        check("exactly 2 attempts were made", flaky.calls == 2)
    finally:
        set_playwright_provider(PlaywrightRealProvider())

    print("\n== 5. Timeout (ToolRegistry's timeout mechanism) ==")
    def _slow_query():
        time.sleep(2.0)
        return {"success": True}

    get_tool_registry().register(
        ToolSpec(
            name="playwright_test_slow_query",
            input_schema=create_model("SlowQueryArgs"),
            permissions=["internet_access"],
            retry_policy=RetryPolicy(max_attempts=1),
            timeout_seconds=0.3,
        ),
        _slow_query,
    )
    start = time.monotonic()
    try:
        get_tool_registry().invoke("playwright_test_slow_query", agent_name="playwright_agent")
        timed_out = False
    except TimeoutError:
        timed_out = True
    elapsed = time.monotonic() - start
    check("a slow query times out per the tool's configured timeout_seconds", timed_out)
    check("did not wait for the full 2s operation to finish", elapsed < 1.5)

    print("\n== 6. Graceful Failure (real provider - playwright not installed) ==")
    real_health = pw_agent.run_playwright_operation("playwright_health_check", venture_id="v-test")
    check("real provider health check completes without crashing", real_health["event"] == "playwright_completed")
    check("reports unhealthy (playwright not installed in this sandbox)", real_health["healthy"] is False)
    check("reports not installed", real_health["installed"] is False)
    print(f"     real provider error (expected): {real_health.get('error')}")

    print("\n== 7. Fake Provider: full browser lifecycle ==")
    set_playwright_provider(FakePlaywrightProvider())
    try:
        launch = pw_agent.run_playwright_operation("playwright_launch_browser", venture_id="v-test")
        check("launch_browser returned a browser_id", bool(launch.get("browser_id")))
        bid = launch["browser_id"]

        before = pw_agent.run_playwright_operation("playwright_extract_html", venture_id="v-test", browser_id=bid)
        check("extract_html before open_url fails gracefully", before["event"] == "playwright_failed")

        opened = pw_agent.run_playwright_operation("playwright_open_url", venture_id="v-test", browser_id=bid, url="https://example.com")
        check("open_url succeeded", opened["success"])

        html = pw_agent.run_playwright_operation("playwright_extract_html", venture_id="v-test", browser_id=bid)
        check("extract_html returns content referencing the opened url", "example.com" in html["html"])

        text = pw_agent.run_playwright_operation("playwright_extract_text", venture_id="v-test", browser_id=bid)
        check("extract_text returns non-empty text", bool(text["text"]))

        shot = pw_agent.run_playwright_operation("playwright_screenshot", venture_id="v-test", browser_id=bid)
        check("screenshot returns base64 data", bool(shot["screenshot_base64"]))

        fill = pw_agent.run_playwright_operation("playwright_fill_form", venture_id="v-test", browser_id=bid, fields={"#email": "a@b.com"})
        check("fill_form succeeded", fill["success"])

        click_ok = pw_agent.run_playwright_operation("playwright_click_element", venture_id="v-test", browser_id=bid, selector="#submit")
        check("click_element succeeded on a normal selector", click_ok["success"])

        click_fail = pw_agent.run_playwright_operation("playwright_click_element", venture_id="v-test", browser_id=bid, selector="#does-not-exist")
        check("click_element fails gracefully on a missing selector", click_fail["event"] == "playwright_failed")

        wait_ok = pw_agent.run_playwright_operation("playwright_wait_for_selector", venture_id="v-test", browser_id=bid, selector="#ready")
        check("wait_for_selector succeeded on a normal selector", wait_ok["success"])

        wait_fail = pw_agent.run_playwright_operation("playwright_wait_for_selector", venture_id="v-test", browser_id=bid, selector="#does-not-exist")
        check("wait_for_selector fails gracefully on a missing selector", wait_fail["event"] == "playwright_failed")

        print("\n== 8. Approval Interrupts: execute_javascript (high risk) ==")

        class _S(TypedDict, total=False):
            history: Annotated[list, operator.add]
            browser_id: str

        def js_node(state: dict) -> dict:
            entry = pw_agent.run_playwright_operation(
                "playwright_execute_javascript", venture_id="v-test", browser_id=state["browser_id"], script="document.title",
            )
            return {"history": [entry]}

        graph = StateGraph(_S)
        graph.add_node("run_js", js_node)
        graph.add_edge(START, "run_js")
        graph.add_edge("run_js", END)
        compiled = graph.compile(checkpointer=InMemorySaver())

        reject_thread = f"pw-js-reject-{uuid.uuid4().hex[:8]}"
        config_reject = {"configurable": {"thread_id": reject_thread}}
        first = compiled.invoke({"history": [], "browser_id": bid}, config=config_reject)
        check("execute_javascript paused for human approval (real interrupt)", "__interrupt__" in first)
        check("interrupt carries the playwright_execute_javascript action", first["__interrupt__"][0].value["action"] == "playwright_execute_javascript")

        rejected = compiled.invoke(Command(resume={"approved": False, "reason": "not now"}), config=config_reject)
        check("rejected execute_javascript does not run", rejected["history"][0]["event"] == "playwright_failed")

        approve_thread = f"pw-js-approve-{uuid.uuid4().hex[:8]}"
        config_approve = {"configurable": {"thread_id": approve_thread}}
        compiled.invoke({"history": [], "browser_id": bid}, config=config_approve)
        approved = compiled.invoke(Command(resume={"approved": True, "reason": "reviewed"}), config=config_approve)
        check("resumed past the gate after approval", "__interrupt__" not in approved)
        check("execute_javascript ran after approval", approved["history"][0]["event"] == "playwright_completed")
        check("js_result reflects the executed script", approved["history"][0]["js_result"] == "executed: document.title")

        print("\n== 9. Close browser + use-after-close ==")
        close = pw_agent.run_playwright_operation("playwright_close_browser", venture_id="v-test", browser_id=bid)
        check("close_browser succeeded", close["success"])

        after_close = pw_agent.run_playwright_operation("playwright_extract_html", venture_id="v-test", browser_id=bid)
        check("using a closed browser fails gracefully", after_close["event"] == "playwright_failed")
    finally:
        set_playwright_provider(PlaywrightRealProvider())

    print("\n== 10. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    pw_agent.run_playwright_operation("playwright_health_check", venture_id="v-test")
    check("published playwright_started", "playwright_started" in seen_events)
    check("published playwright_completed", "playwright_completed" in seen_events)

    seen_events.clear()
    set_playwright_provider(_FlakyProvider(fail_times=10))
    try:
        pw_agent.run_playwright_operation("playwright_health_check", venture_id="v-test")
    finally:
        set_playwright_provider(PlaywrightRealProvider())
    check("published playwright_failed", "playwright_failed" in seen_events)

    print("\n== 11. Manager-callable node shape ==")
    delta = pw_agent.playwright_agent_node({"venture_id": "v-test"})
    check("playwright_agent_node returns a history delta", "history" in delta and len(delta["history"]) == 1)

    print("\nAll Phase 3 Component 6 checks passed.")


if __name__ == "__main__":
    main()
