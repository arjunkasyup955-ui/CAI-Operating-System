import logging
import re
from pathlib import Path

import tools.web.search  # noqa: F401  (import registers the web_search tool)
from core.registries import get_agent_registry, get_tool_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.search")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

# Tavily hard-rejects queries over 400 chars with a 400 Bad Request; a long,
# multi-paragraph idea description used as a literal query used to fail every
# query-based provider and silently cascade to neutral-default scores
# downstream (the Bug #10 fingerprint, retriggered via a new path). Margin
# kept below the actual 400-char cap. Only the search query is shortened -
# state["idea"] is untouched, so every other research stage still analyzes
# the full idea text.
_MAX_QUERY_CHARS = 350


def _build_search_query(idea: str) -> str:
    idea = idea.strip()
    if len(idea) <= _MAX_QUERY_CHARS:
        return idea

    first_sentence = re.split(r"(?<=[.!?])\s", idea, maxsplit=1)[0]
    if first_sentence and len(first_sentence) <= _MAX_QUERY_CHARS:
        return first_sentence

    truncated = idea[:_MAX_QUERY_CHARS]
    last_space = truncated.rfind(" ")
    return truncated[:last_space] if last_space > 0 else truncated


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
    query = _build_search_query(idea)

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
