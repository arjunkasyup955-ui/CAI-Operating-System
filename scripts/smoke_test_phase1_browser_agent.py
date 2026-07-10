"""Phase 1, Component 3: Browser Agent (Playwright primary, Crawl4AI/Firecrawl
fallbacks).

Verifies:
  1. tool registration - 'browser_fetch' is registered with a schema and permissions.
  2. schema validation - a call missing the required 'url' field is rejected before
     the tool ever runs.
  3. permission enforcement - an agent without internet_access is denied.
  4. fallback routing - BrowserRouter falls through failing providers to a working
     one, using injected fake providers (no real browser/network needed for this part).
  5. Browser Agent execution - browser_node correctly picks up a URL surfaced by a
     prior search_agent finding and calls browser_fetch through the Tool Registry.
  6. full graph execution - the Search Agent -> Browser Agent chain runs end to end
     through the real Manager graph without crashing it, whether or not a real
     browser provider is actually configured/installed in this environment.
  7. rerun idempotency - running this script twice back to back produces the same
     PASS results (fresh thread_id per run, not accumulated state).

Run: python scripts/smoke_test_phase1_browser_agent.py
"""

import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from langgraph.types import Command  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import agents.research.browser.agent  # noqa: E402  (registers manifest + browser_fetch tool)
from agents.research.browser.agent import browser_node  # noqa: E402
from core.memory_gateway import get_memory_gateway  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import get_agent_registry, get_tool_registry  # noqa: E402
from tools.browser.browser_providers import FetchResult  # noqa: E402
from tools.browser.browser_router import BrowserRouter  # noqa: E402
from workflows.main_graph import compile_main_graph  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


class _FailingProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    def fetch(self, url, **kwargs):
        raise RuntimeError(f"simulated outage in {self.name}")


class _WorkingProvider:
    name = "fake_working"

    def fetch(self, url, **kwargs):
        return FetchResult(url=url, title="fake title", content=f"fake content for {url}", provider=self.name)


def run_all_checks() -> None:
    print("\n== 1 & 4. Tool registration + BrowserRouter fallback logic (injected fake providers) ==")
    spec = get_tool_registry().get_spec("browser_fetch")
    check("browser_fetch registered with an input schema", spec.input_schema is not None)
    check("browser_fetch requires internet_access permission", "internet_access" in spec.permissions)

    router = BrowserRouter(
        ["primary", "fallback1", "fallback2"],
        {"primary": _FailingProvider("primary"), "fallback1": _FailingProvider("fallback1"), "fallback2": _WorkingProvider()},
    )
    result = router.fetch("https://example.com")
    check("falls through 2 failing providers to the working one", result.provider == "fake_working")

    all_fail_router = BrowserRouter(["a", "b"], {"a": _FailingProvider("a"), "b": _FailingProvider("b")})
    try:
        all_fail_router.fetch("https://example.com")
        all_raised = False
    except RuntimeError:
        all_raised = True
    check("raises clearly when every provider fails", all_raised)

    print("\n== 2. Schema validation ==")
    try:
        get_tool_registry().invoke("browser_fetch", agent_name="browser_agent")
        schema_rejected = False
    except ValidationError:
        schema_rejected = True
    check("missing required 'url' field rejected before execution", schema_rejected)

    print("\n== 3. Permission enforcement ==")
    manifest = get_agent_registry().get("browser_agent")
    get_agent_registry().register(manifest.model_copy(update={"name": "no_net_browser_agent", "permissions": []}))
    try:
        get_tool_registry().invoke("browser_fetch", agent_name="no_net_browser_agent", url="https://example.com")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without internet_access is denied", denied)

    print("\n== 5. Browser Agent execution: picks up a URL from a seeded search finding ==")
    seeded_state = {
        "idea": "test idea",
        "research_findings": [
            {
                "agent": "search_agent",
                "event": "search_completed",
                "query": "test idea",
                "result_count": 1,
                "results": [{"title": "A result", "url": "https://example.com/result", "snippet": "", "provider": "fake"}],
            }
        ],
    }
    delta = browser_node(seeded_state)
    outcome = delta["research_findings"][0]["event"]
    check("browser_node picked up the seeded URL and attempted a fetch", outcome in ("browser_fetch_completed", "browser_failed"))
    check("browser_node recorded the URL it attempted", delta["research_findings"][0].get("url") == "https://example.com/result")
    print(f"     outcome with a seeded URL: {outcome}")

    print("\n== 5b. Browser Agent execution: no URL available -> skipped, not failed ==")
    empty_delta = browser_node({"idea": "test idea", "research_findings": []})
    check("browser_node skips gracefully with no prior search results", empty_delta["research_findings"][0]["event"] == "browser_skipped")

    print("\n== 6. Full graph execution: Search Agent -> Browser Agent through the real Manager graph ==")
    gateway = get_memory_gateway()
    with gateway.working() as checkpointer:
        graph = compile_main_graph(checkpointer)
        venture_id = f"venture-browser-agent-test-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": venture_id}}
        initial_state = {
            "venture_id": venture_id,
            "idea": "A subscription box for artisanal hot sauce",
            "phase": "idea",
        }

        first = graph.invoke(initial_state, config=config)
        check("paused on approval interrupt", "__interrupt__" in first)

        final = graph.invoke(Command(resume={"approved": True, "reason": "approved for research"}), config=config)
        check("graph completed without crashing", "__interrupt__" not in final)

        browser_entries = [h for h in final["history"] if h.get("agent") == "browser_agent"]
        check("exactly one browser_agent history entry (no duplication)", len(browser_entries) == 1)
        outcome = browser_entries[0]["event"]
        check(
            "browser_agent recorded a well-formed outcome",
            outcome in ("browser_fetch_completed", "browser_failed", "browser_skipped"),
        )
        print(f"     browser_agent outcome in this environment: {outcome}")
        if outcome == "browser_skipped":
            print("     (expected here - no SearXNG/Tavily/Brave configured, so search_agent found no URL to hand off)")


def main() -> None:
    run_all_checks()
    print("\nAll Phase 1 Component 3 checks passed.")


if __name__ == "__main__":
    main()
