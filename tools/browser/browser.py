import logging

from pydantic import BaseModel

from core.event_bus import AFOSEvent, get_event_bus
from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.browser.browser_router import get_browser_router

logger = logging.getLogger("afos.tools.browser_fetch")


class BrowserFetchArgs(BaseModel):
    url: str


def browser_fetch(url: str) -> dict:
    """Registered as the 'browser_fetch' tool. Fallback across providers happens
    inside BrowserRouter, not here - ToolRegistry's own retry_policy is set to 1
    attempt for the same reason as web_search: a single pass already tries every
    configured provider, so retrying the whole chain would mostly just add latency.
    """
    bus = get_event_bus()
    try:
        result = get_browser_router().fetch(url)
    except Exception as exc:
        bus.publish(
            AFOSEvent(
                type="browser.all_providers_failed",
                source_agent="browser_fetch",
                payload={"url": url, "error": str(exc)},
            )
        )
        raise

    bus.publish(
        AFOSEvent(
            type="browser.fetch_completed",
            source_agent="browser_fetch",
            payload={"provider": result.provider, "url": url, "content_length": len(result.content)},
        )
    )
    return result.model_dump()


get_tool_registry().register(
    ToolSpec(
        name="browser_fetch",
        description="Fetch and extract a web page via Playwright (primary), falling back to Crawl4AI then Firecrawl.",
        input_schema=BrowserFetchArgs,
        permissions=["internet_access"],
        retry_policy=RetryPolicy(max_attempts=1),
        timeout_seconds=30.0,
        cost_per_call_usd=0.0,
    ),
    browser_fetch,
)
