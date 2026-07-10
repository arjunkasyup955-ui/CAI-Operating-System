from pydantic import BaseModel

from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.searxng.searxng_providers import get_searxng_provider

_RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_seconds=1.0)
_TIMEOUT_SECONDS = 20.0


class HealthCheckArgs(BaseModel):
    pass


class WebSearchArgs(BaseModel):
    query: str
    category: str = "general"
    language: str = "all"
    safe_search: int = 1
    max_results: int = 10


class NewsSearchArgs(BaseModel):
    query: str
    language: str = "all"
    safe_search: int = 1
    max_results: int = 10


class ImageSearchArgs(BaseModel):
    query: str
    language: str = "all"
    safe_search: int = 1
    max_results: int = 10


def searxng_health_check() -> dict:
    return get_searxng_provider().health_check().model_dump()


def searxng_web_search(query: str, category: str = "general", language: str = "all", safe_search: int = 1, max_results: int = 10) -> dict:
    return get_searxng_provider().search(query, category, language, safe_search, max_results).model_dump()


def searxng_news_search(query: str, language: str = "all", safe_search: int = 1, max_results: int = 10) -> dict:
    return get_searxng_provider().search(query, "news", language, safe_search, max_results).model_dump()


def searxng_image_search(query: str, language: str = "all", safe_search: int = 1, max_results: int = 10) -> dict:
    return get_searxng_provider().search(query, "images", language, safe_search, max_results).model_dump()


_TOOLS: list[tuple[str, str, type[BaseModel], object]] = [
    ("searxng_health_check", "Check whether the SearXNG instance is reachable", HealthCheckArgs, searxng_health_check),
    ("searxng_web_search", "General web search with selectable category/language/safe_search/max_results", WebSearchArgs, searxng_web_search),
    ("searxng_news_search", "News-category search", NewsSearchArgs, searxng_news_search),
    ("searxng_image_search", "Image-category search", ImageSearchArgs, searxng_image_search),
]

for _name, _description, _schema, _func in _TOOLS:
    get_tool_registry().register(
        ToolSpec(
            name=_name,
            description=_description,
            input_schema=_schema,
            permissions=["internet_access"],
            retry_policy=_RETRY_POLICY,
            timeout_seconds=_TIMEOUT_SECONDS,
            cost_per_call_usd=0.0,
        ),
        _func,
    )
