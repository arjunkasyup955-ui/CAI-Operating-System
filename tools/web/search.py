import logging

from pydantic import BaseModel

from core.event_bus import AFOSEvent, get_event_bus
from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.web.search_router import get_search_router

logger = logging.getLogger("afos.tools.web_search")


class WebSearchArgs(BaseModel):
    query: str
    max_results: int = 5


def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Registered as the 'web_search' tool. Fallback across providers happens inside
    SearchRouter, not here - ToolRegistry's own retry_policy is set to 1 attempt because
    retrying the whole provider chain adds little (a single pass already tries every
    configured provider) and would otherwise multiply latency on a bad day.
    """
    bus = get_event_bus()
    try:
        results = get_search_router().search(query, max_results=max_results)
    except Exception as exc:
        bus.publish(
            AFOSEvent(
                type="search.all_providers_failed",
                source_agent="web_search",
                payload={"query": query, "error": str(exc)},
            )
        )
        raise

    bus.publish(
        AFOSEvent(
            type="search.completed",
            source_agent="web_search",
            payload={"provider": results[0].provider if results else "none", "query": query, "result_count": len(results)},
        )
    )
    return [r.model_dump() for r in results]


get_tool_registry().register(
    ToolSpec(
        name="web_search",
        description="Search the web via SearXNG (primary), falling back to Tavily then Brave Search.",
        input_schema=WebSearchArgs,
        permissions=["internet_access"],
        retry_policy=RetryPolicy(max_attempts=1),
        timeout_seconds=20.0,
        cost_per_call_usd=0.0,
    ),
    web_search,
)
