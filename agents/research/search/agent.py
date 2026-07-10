import logging
from pathlib import Path

import tools.web.search  # noqa: F401  (import registers the web_search tool)
from core.registries import get_agent_registry, get_tool_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.search")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")


def search_node(state: VentureState) -> dict:
    """First real worker inside the Research Supervisor. Communicates only through the
    Tool Registry (the 'web_search' tool) and the Event Bus (via that tool's own
    events plus ToolRegistry's automatic tool.invoked event) - never imports a search
    provider or the Event Bus to publish anything itself.

    A total search failure (e.g. no provider reachable/configured) is recorded in
    state rather than raised, so one worker's outage doesn't crash the whole
    Research Supervisor subgraph.
    """
    idea = state.get("idea", "")
    query = idea

    try:
        results = get_tool_registry().invoke("web_search", agent_name="search_agent", query=query, max_results=5)
        entry = {
            "agent": "search_agent",
            "event": "search_completed",
            "query": query,
            "result_count": len(results),
            "results": results,
        }
    except Exception as exc:
        logger.warning("search_agent: web_search failed for query '%s': %s", query, exc)
        entry = {"agent": "search_agent", "event": "search_failed", "query": query, "error": str(exc)}

    return {
        "research_findings": [entry],
        "history": [entry],
    }
