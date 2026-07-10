"""Phase 3, Component 7: Crawl4AI Integration.

Verifies:
  - Registration (agent + 7 tools)
  - Permissions (internet_access, enforced)
  - Schema validation
  - Retry (ToolRegistry's existing RetryPolicy, via a flaky fake provider)
  - Timeout (ToolRegistry's timeout mechanism, via a deliberately slow function)
  - Graceful failure (real provider - crawl4ai package not installed - never
    crashes)
  - Fake provider crawl lifecycle (crawl_url -> genuine cache hit -> forced
    use_cache=False refetch -> crawl_urls -> extract_markdown/links/metadata/
    structured, deterministic, in-memory, with a real per-URL cache, not a stub)
  - Event publishing (crawl4ai_started/completed/failed)
  - Manager node integration
  - Regression of every previous component (run separately, see the full suite this
    script is part of)

Run: python scripts/smoke_test_phase3_crawl4ai.py
"""

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from pydantic import ValidationError, create_model  # noqa: E402

import agents.infrastructure.crawl4ai.agent as c4a_agent  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import ToolSpec, get_agent_registry, get_tool_registry  # noqa: E402
from core.registries.tool_registry import RetryPolicy  # noqa: E402
from tools.crawl4ai.crawl4ai_providers import (  # noqa: E402
    Crawl4AIHealthStatus,
    Crawl4AIRealProvider,
    FakeCrawl4AIProvider,
    set_crawl4ai_provider,
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

    def health_check(self) -> Crawl4AIHealthStatus:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("transient failure")
        return Crawl4AIHealthStatus(healthy=True, installed=True)


def main() -> None:
    expected_tools = {
        "crawl4ai_health_check", "crawl4ai_crawl_url", "crawl4ai_crawl_urls",
        "crawl4ai_extract_markdown", "crawl4ai_extract_structured", "crawl4ai_extract_links",
        "crawl4ai_extract_metadata",
    }

    print("\n== 1. Registration (agent + 7 tools) ==")
    manifest = get_agent_registry().get("crawl4ai_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares internet_access permission", set(manifest.permissions) == {"internet_access"})
    check("declares all 7 tools", set(manifest.tools) == expected_tools)
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Permissions ==")
    for tool_name in expected_tools:
        spec = get_tool_registry().get_spec(tool_name)
        check(f"{tool_name} requires internet_access", "internet_access" in spec.permissions)

    manifest_no_perms = manifest.model_copy(update={"name": "no_net_crawl4ai_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("crawl4ai_health_check", agent_name="no_net_crawl4ai_agent")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without internet_access is denied", denied)

    try:
        get_tool_registry().invoke("crawl4ai_health_check", agent_name="git_agent")
        denied_other = False
    except PermissionDeniedError:
        denied_other = True
    check("an unrelated registered agent (git_agent) is denied", denied_other)

    print("\n== 3. Schema Validation ==")
    try:
        get_tool_registry().invoke("crawl4ai_crawl_url", agent_name="crawl4ai_agent")
        rejected = False
    except ValidationError:
        rejected = True
    check("missing required url rejected", rejected)

    try:
        get_tool_registry().invoke("crawl4ai_extract_structured", agent_name="crawl4ai_agent", url="https://example.com")
        rejected2 = False
    except ValidationError:
        rejected2 = True
    check("missing required extraction_schema rejected", rejected2)

    print("\n== 4. Retry Behaviour (ToolRegistry's existing RetryPolicy) ==")
    flaky = _FlakyProvider(fail_times=1)
    set_crawl4ai_provider(flaky)
    try:
        retry_entry = c4a_agent.run_crawl4ai_operation("crawl4ai_health_check", venture_id="v-test")
        check("retried past one transient failure and completed", retry_entry["event"] == "crawl4ai_completed")
        check("exactly 2 attempts were made", flaky.calls == 2)
    finally:
        set_crawl4ai_provider(Crawl4AIRealProvider())

    print("\n== 5. Timeout (ToolRegistry's timeout mechanism) ==")
    def _slow_query():
        time.sleep(2.0)
        return {"success": True}

    get_tool_registry().register(
        ToolSpec(
            name="crawl4ai_test_slow_query",
            input_schema=create_model("SlowQueryArgs"),
            permissions=["internet_access"],
            retry_policy=RetryPolicy(max_attempts=1),
            timeout_seconds=0.3,
        ),
        _slow_query,
    )
    start = time.monotonic()
    try:
        get_tool_registry().invoke("crawl4ai_test_slow_query", agent_name="crawl4ai_agent")
        timed_out = False
    except TimeoutError:
        timed_out = True
    elapsed = time.monotonic() - start
    check("a slow query times out per the tool's configured timeout_seconds", timed_out)
    check("did not wait for the full 2s operation to finish", elapsed < 1.5)

    print("\n== 6. Graceful Failure (real provider - crawl4ai not installed) ==")
    real_health = c4a_agent.run_crawl4ai_operation("crawl4ai_health_check", venture_id="v-test")
    check("real provider health check completes without crashing", real_health["event"] == "crawl4ai_completed")
    check("reports unhealthy (crawl4ai not installed in this sandbox)", real_health["healthy"] is False)
    check("reports not installed", real_health["installed"] is False)
    print(f"     real provider error (expected): {real_health.get('error')}")

    print("\n== 7. Fake Provider: crawl lifecycle + cache support ==")
    fake = FakeCrawl4AIProvider()
    set_crawl4ai_provider(fake)
    try:
        r1 = c4a_agent.run_crawl4ai_operation("crawl4ai_crawl_url", venture_id="v-test", url="https://example.com")
        check("crawl_url succeeded on first (uncached) call", r1["success"])
        check("first call is not marked cached", r1["pages"][0]["cached"] is False)
        check("exactly one real fetch happened", fake.fetch_count == 1)

        r2 = c4a_agent.run_crawl4ai_operation("crawl4ai_crawl_url", venture_id="v-test", url="https://example.com")
        check("second call with use_cache=True is served from cache", r2["pages"][0]["cached"] is True)
        check("cache hit did not trigger a real fetch", fake.fetch_count == 1)

        r3 = c4a_agent.run_crawl4ai_operation("crawl4ai_crawl_url", venture_id="v-test", url="https://example.com", use_cache=False)
        check("use_cache=False forces a real refetch", r3["pages"][0]["cached"] is False)
        check("fetch count incremented on forced refetch", fake.fetch_count == 2)

        multi = c4a_agent.run_crawl4ai_operation("crawl4ai_crawl_urls", venture_id="v-test", urls=["https://a.com", "https://b.com"])
        check("crawl_urls returned both pages", {p["url"] for p in multi["pages"]} == {"https://a.com", "https://b.com"})
        check("crawl_urls fetched both new urls", fake.fetch_count == 4)

        md = c4a_agent.run_crawl4ai_operation("crawl4ai_extract_markdown", venture_id="v-test", url="https://example.com")
        check("extract_markdown returns non-empty markdown", bool(md["pages"][0]["markdown"]))

        links = c4a_agent.run_crawl4ai_operation("crawl4ai_extract_links", venture_id="v-test", url="https://example.com")
        check("extract_links returns 2 fake links", len(links["pages"][0]["links"]) == 2)

        meta = c4a_agent.run_crawl4ai_operation("crawl4ai_extract_metadata", venture_id="v-test", url="https://example.com")
        check("extract_metadata returns a title", "title" in meta["pages"][0]["metadata"])

        structured = c4a_agent.run_crawl4ai_operation(
            "crawl4ai_extract_structured", venture_id="v-test", url="https://example.com",
            extraction_schema={"headline": "h1.title"},
        )
        check("extract_structured applies the caller's schema", structured["pages"][0]["structured"]["headline"] == "fake-value-for-h1.title")
    finally:
        set_crawl4ai_provider(Crawl4AIRealProvider())

    print("\n== 8. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    c4a_agent.run_crawl4ai_operation("crawl4ai_health_check", venture_id="v-test")
    check("published crawl4ai_started", "crawl4ai_started" in seen_events)
    check("published crawl4ai_completed", "crawl4ai_completed" in seen_events)

    seen_events.clear()
    set_crawl4ai_provider(_FlakyProvider(fail_times=10))
    try:
        c4a_agent.run_crawl4ai_operation("crawl4ai_health_check", venture_id="v-test")
    finally:
        set_crawl4ai_provider(Crawl4AIRealProvider())
    check("published crawl4ai_failed", "crawl4ai_failed" in seen_events)

    print("\n== 9. Manager-callable node shape ==")
    delta = c4a_agent.crawl4ai_agent_node({"venture_id": "v-test"})
    check("crawl4ai_agent_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
    check("node result reflects the health check operation", delta["history"][0]["operation"] == "crawl4ai_health_check")

    print("\nAll Phase 3 Component 7 checks passed.")


if __name__ == "__main__":
    main()
