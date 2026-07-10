"""Phase 3, Component 8: SearXNG Integration.

Verifies:
  - Registration (agent + 4 tools)
  - Permissions (internet_access, enforced)
  - Schema validation
  - Retry (ToolRegistry's existing RetryPolicy, via a flaky fake provider)
  - Timeout (ToolRegistry's timeout mechanism, via a deliberately slow function)
  - Graceful failure (real provider - no local SearXNG instance reachable - never
    crashes)
  - Fake provider search lifecycle (web/news/image search, deterministic, in-memory)
  - Category filtering (web search default "general", news/image tools force
    "news"/"images", and web search accepts an arbitrary override category)
  - Language filtering (language is genuinely threaded through to the provider)
  - Safe search + result limiting (max_results genuinely truncates the returned
    list while number_of_results still reflects the total available)
  - Event publishing (searxng_started/completed/failed)
  - Manager node integration
  - Regression of every previous component (run separately, see the full suite this
    script is part of)

Run: python scripts/smoke_test_phase3_searxng.py
"""

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from pydantic import ValidationError, create_model  # noqa: E402

import agents.infrastructure.searxng.agent as sx_agent  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import ToolSpec, get_agent_registry, get_tool_registry  # noqa: E402
from core.registries.tool_registry import RetryPolicy  # noqa: E402
from tools.searxng.searxng_providers import (  # noqa: E402
    FakeSearXNGProvider,
    SearXNGHealthStatus,
    SearXNGRESTProvider,
    set_searxng_provider,
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

    def health_check(self) -> SearXNGHealthStatus:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("transient failure")
        return SearXNGHealthStatus(healthy=True, base_url="flaky")


def main() -> None:
    expected_tools = {"searxng_health_check", "searxng_web_search", "searxng_news_search", "searxng_image_search"}

    print("\n== 1. Registration (agent + 4 tools) ==")
    manifest = get_agent_registry().get("searxng_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares internet_access permission", set(manifest.permissions) == {"internet_access"})
    check("declares all 4 tools", set(manifest.tools) == expected_tools)
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Permissions ==")
    for tool_name in expected_tools:
        spec = get_tool_registry().get_spec(tool_name)
        check(f"{tool_name} requires internet_access", "internet_access" in spec.permissions)

    manifest_no_perms = manifest.model_copy(update={"name": "no_net_searxng_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("searxng_health_check", agent_name="no_net_searxng_agent")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without internet_access is denied", denied)

    try:
        get_tool_registry().invoke("searxng_health_check", agent_name="git_agent")
        denied_other = False
    except PermissionDeniedError:
        denied_other = True
    check("an unrelated registered agent (git_agent) is denied", denied_other)

    print("\n== 3. Schema Validation ==")
    try:
        get_tool_registry().invoke("searxng_web_search", agent_name="searxng_agent")
        rejected = False
    except ValidationError:
        rejected = True
    check("missing required query rejected", rejected)

    print("\n== 4. Retry Behaviour (ToolRegistry's existing RetryPolicy) ==")
    flaky = _FlakyProvider(fail_times=1)
    set_searxng_provider(flaky)
    try:
        retry_entry = sx_agent.run_searxng_operation("searxng_health_check", venture_id="v-test")
        check("retried past one transient failure and completed", retry_entry["event"] == "searxng_completed")
        check("exactly 2 attempts were made", flaky.calls == 2)
    finally:
        set_searxng_provider(SearXNGRESTProvider())

    print("\n== 5. Timeout (ToolRegistry's timeout mechanism) ==")
    def _slow_query():
        time.sleep(2.0)
        return {"success": True}

    get_tool_registry().register(
        ToolSpec(
            name="searxng_test_slow_query",
            input_schema=create_model("SlowQueryArgs"),
            permissions=["internet_access"],
            retry_policy=RetryPolicy(max_attempts=1),
            timeout_seconds=0.3,
        ),
        _slow_query,
    )
    start = time.monotonic()
    try:
        get_tool_registry().invoke("searxng_test_slow_query", agent_name="searxng_agent")
        timed_out = False
    except TimeoutError:
        timed_out = True
    elapsed = time.monotonic() - start
    check("a slow query times out per the tool's configured timeout_seconds", timed_out)
    check("did not wait for the full 2s operation to finish", elapsed < 1.5)

    print("\n== 6. Graceful Failure (real provider - no local SearXNG instance) ==")
    real_health = sx_agent.run_searxng_operation("searxng_health_check", venture_id="v-test")
    check("real provider health check completes without crashing", real_health["event"] == "searxng_completed")
    check("reports unhealthy (no SearXNG instance running in this sandbox)", real_health["healthy"] is False)
    print(f"     real provider error (expected): {real_health.get('error')}")

    print("\n== 7. Fake Provider: search lifecycle ==")
    fake = FakeSearXNGProvider()
    set_searxng_provider(fake)
    try:
        web = sx_agent.run_searxng_operation("searxng_web_search", venture_id="v-test", query="startup ideas", max_results=3)
        check("web_search succeeded", web["success"])
        check("web_search defaults to category 'general'", web["results"][0]["category"] == "general")

        news = sx_agent.run_searxng_operation("searxng_news_search", venture_id="v-test", query="ai news", max_results=5)
        check("news_search forces category 'news'", news["results"][0]["category"] == "news")

        imgs = sx_agent.run_searxng_operation("searxng_image_search", venture_id="v-test", query="cats", max_results=2)
        check("image_search forces category 'images'", imgs["results"][0]["category"] == "images")
        check("image results carry a thumbnail", bool(imgs["results"][0]["thumbnail"]))

        print("\n== 8. Category Filtering ==")
        custom = sx_agent.run_searxng_operation(
            "searxng_web_search", venture_id="v-test", query="papers", category="science", max_results=1,
        )
        check("web_search accepts an arbitrary category override", fake.calls[-1]["category"] == "science")
        check("result reflects the overridden category", custom["results"][0]["category"] == "science")

        print("\n== 9. Language Filtering ==")
        sx_agent.run_searxng_operation("searxng_web_search", venture_id="v-test", query="bonjour", language="fr", max_results=1)
        check("language is genuinely threaded through to the provider", fake.calls[-1]["language"] == "fr")

        print("\n== 10. Safe Search + Result Limiting ==")
        sx_agent.run_searxng_operation("searxng_web_search", venture_id="v-test", query="test", safe_search=2, max_results=1)
        check("safe_search is genuinely threaded through to the provider", fake.calls[-1]["safe_search"] == 2)

        limited = sx_agent.run_searxng_operation("searxng_web_search", venture_id="v-test", query="test", max_results=4)
        check("max_results genuinely truncates the returned list", len(limited["results"]) == 4)
        check("number_of_results still reflects the total available, not the truncated count", limited["number_of_results"] == 20)
    finally:
        set_searxng_provider(SearXNGRESTProvider())

    print("\n== 11. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    sx_agent.run_searxng_operation("searxng_health_check", venture_id="v-test")
    check("published searxng_started", "searxng_started" in seen_events)
    check("published searxng_completed", "searxng_completed" in seen_events)

    seen_events.clear()
    set_searxng_provider(_FlakyProvider(fail_times=10))
    try:
        sx_agent.run_searxng_operation("searxng_health_check", venture_id="v-test")
    finally:
        set_searxng_provider(SearXNGRESTProvider())
    check("published searxng_failed", "searxng_failed" in seen_events)

    print("\n== 12. Manager-callable node shape ==")
    delta = sx_agent.searxng_agent_node({"venture_id": "v-test"})
    check("searxng_agent_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
    check("node result reflects the health check operation", delta["history"][0]["operation"] == "searxng_health_check")

    print("\nAll Phase 3 Component 8 checks passed.")


if __name__ == "__main__":
    main()
