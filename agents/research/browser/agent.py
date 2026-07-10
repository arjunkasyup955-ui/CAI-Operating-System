import logging
from pathlib import Path
from typing import Any

import tools.browser.browser  # noqa: F401  (import registers the browser_fetch tool)
from core.registries import get_agent_registry, get_tool_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.browser")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")


def _pick_url_from_findings(state: VentureState) -> str | None:
    """Looks for the first URL the Search Agent already found - this is the natural
    hand-off between the two workers, not new cross-agent logic invented here. Never
    reaches into search internals; only reads the shared research_findings state.
    """
    for finding in state.get("research_findings", []):
        if finding.get("agent") == "search_agent" and finding.get("event") == "search_completed":
            results = finding.get("results") or []
            if results:
                return results[0].get("url")
    return None


def browser_node(state: VentureState) -> dict:
    """Second real worker inside the Research Supervisor. Communicates only through
    the Tool Registry (the 'browser_fetch' tool) - never imports Playwright, Crawl4AI,
    Firecrawl, or any browser automation library directly.

    A total fetch failure (e.g. no provider installed/configured/reachable) is
    recorded as 'browser_failed' in state rather than raised, so this worker's
    outage never crashes the Research Supervisor subgraph.
    """
    url = _pick_url_from_findings(state)
    if not url:
        entry: dict[str, Any] = {
            "agent": "browser_agent",
            "event": "browser_skipped",
            "reason": "no URL available from prior research",
        }
        return {"research_findings": [entry], "history": [entry]}

    try:
        result = get_tool_registry().invoke("browser_fetch", agent_name="browser_agent", url=url)
        fetched_content = result.get("content", "")
        entry = {
            "agent": "browser_agent",
            "event": "browser_fetch_completed",
            "url": url,
            "content_length": len(fetched_content),
            # Truncated, not the full page - just enough for a downstream analysis
            # agent (e.g. Market Intelligence, Component 4) to work with. Added here
            # rather than a separate re-fetch, since Browser Agent is the only thing
            # allowed to touch a browser provider.
            "content": fetched_content[:5000],
        }
    except Exception as exc:
        logger.warning("browser_agent: browser_fetch failed for url '%s': %s", url, exc)
        entry = {"agent": "browser_agent", "event": "browser_failed", "url": url, "error": str(exc)}

    return {
        "research_findings": [entry],
        "history": [entry],
    }
