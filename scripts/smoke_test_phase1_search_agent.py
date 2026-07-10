"""Phase 1, Component 2: Search Agent (SearXNG primary, Tavily/Brave fallbacks).

Verifies:
  1. SearchRouter's fallback logic in isolation, with injected fake providers (fast,
     deterministic, no network needed) - proves the adapter interface and fallback
     ordering work regardless of which real providers are reachable in this
     environment.
  2. The 'web_search' tool is registered with a schema and goes through the same
     mandatory Tool Registry enforcement (schema validation, permission checks) as
     every other tool - no special-casing for this tool.
  3. The Search Agent, wired as the first real worker inside the Research Supervisor,
     runs end to end through the full Manager graph without crashing it, whether or
     not a real search provider is actually configured in this environment.

Run: python scripts/smoke_test_phase1_search_agent.py
"""

import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from langgraph.types import Command  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import agents.research.search.agent  # noqa: E402  (registers manifest + web_search tool)
from core.memory_gateway import get_memory_gateway  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import get_agent_registry, get_tool_registry  # noqa: E402
from tools.web.search_providers import SearchResult  # noqa: E402
from tools.web.search_router import SearchRouter  # noqa: E402
from workflows.main_graph import compile_main_graph  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


class _FailingProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    def search(self, query, max_results=5, **kwargs):
        raise RuntimeError(f"simulated outage in {self.name}")


class _WorkingProvider:
    name = "fake_working"

    def search(self, query, max_results=5, **kwargs):
        return [SearchResult(title=f"result for {query}", url="https://example.com", snippet="snippet", provider=self.name)]


def main() -> None:
    print("\n== 1. SearchRouter fallback logic (injected fake providers) ==")
    router = SearchRouter(
        ["primary", "fallback1", "fallback2"],
        {"primary": _FailingProvider("primary"), "fallback1": _FailingProvider("fallback1"), "fallback2": _WorkingProvider()},
    )
    results = router.search("test query")
    check("falls through 2 failing providers to the working one", len(results) == 1 and results[0].provider == "fake_working")

    all_fail_router = SearchRouter(["a", "b"], {"a": _FailingProvider("a"), "b": _FailingProvider("b")})
    try:
        all_fail_router.search("x")
        all_raised = False
    except RuntimeError:
        all_raised = True
    check("raises clearly when every provider fails", all_raised)

    print("\n== 2. Tool Registry integration (schema + permissions, no special-casing) ==")
    spec = get_tool_registry().get_spec("web_search")
    check("web_search registered with an input schema", spec.input_schema is not None)
    check("web_search requires internet_access permission", "internet_access" in spec.permissions)

    try:
        get_tool_registry().invoke("web_search", agent_name="search_agent", max_results=3)
        schema_rejected = False
    except ValidationError:
        schema_rejected = True
    check("missing required 'query' field rejected before execution", schema_rejected)

    manifest = get_agent_registry().get("search_agent")
    get_agent_registry().register(manifest.model_copy(update={"name": "no_net_agent", "permissions": []}))
    try:
        get_tool_registry().invoke("web_search", agent_name="no_net_agent", query="x")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without internet_access is denied", denied)

    print("\n== 3. Search Agent wired into Research Supervisor, through the full Manager graph ==")
    gateway = get_memory_gateway()
    with gateway.working() as checkpointer:
        graph = compile_main_graph(checkpointer)
        # Fresh thread_id every run - see the matching note in
        # smoke_test_phase1_research_supervisor.py (persistent SQLite checkpointer +
        # a fixed id would accumulate state across separate script executions).
        venture_id = f"venture-search-agent-test-{uuid.uuid4().hex[:8]}"
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

        search_entries = [h for h in final["history"] if h.get("agent") == "search_agent"]
        check("exactly one search_agent history entry (no duplication)", len(search_entries) == 1)
        outcome = search_entries[0]["event"]
        check("search_agent recorded a well-formed outcome", outcome in ("search_completed", "search_failed"))
        print(f"     search_agent outcome in this environment: {outcome}")
        if outcome == "search_failed":
            print("     (expected here - no SearXNG/Tavily/Brave configured in this sandbox; "
                  "the important thing is the graph did not crash)")

    print("\nAll Phase 1 Component 2 checks passed.")


if __name__ == "__main__":
    main()
