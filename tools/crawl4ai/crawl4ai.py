from typing import Any

from pydantic import BaseModel

from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.crawl4ai.crawl4ai_providers import get_crawl4ai_provider

_RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_seconds=1.0)
_TIMEOUT_SECONDS = 20.0

# Field names deliberately never "name" - see the docstring in
# crawl4ai_providers.py for why.


class HealthCheckArgs(BaseModel):
    pass


class CrawlUrlArgs(BaseModel):
    url: str
    use_cache: bool = True


class CrawlUrlsArgs(BaseModel):
    urls: list[str]
    use_cache: bool = True


class ExtractMarkdownArgs(BaseModel):
    url: str
    use_cache: bool = True


class ExtractStructuredArgs(BaseModel):
    url: str
    extraction_schema: dict[str, str]
    use_cache: bool = True


class ExtractLinksArgs(BaseModel):
    url: str
    use_cache: bool = True


class ExtractMetadataArgs(BaseModel):
    url: str
    use_cache: bool = True


def crawl4ai_health_check() -> dict:
    return get_crawl4ai_provider().health_check().model_dump()


def crawl4ai_crawl_url(url: str, use_cache: bool = True) -> dict:
    return get_crawl4ai_provider().crawl_url(url, use_cache).model_dump()


def crawl4ai_crawl_urls(urls: list[str], use_cache: bool = True) -> dict:
    return get_crawl4ai_provider().crawl_urls(urls, use_cache).model_dump()


def crawl4ai_extract_markdown(url: str, use_cache: bool = True) -> dict:
    return get_crawl4ai_provider().extract_markdown(url, use_cache).model_dump()


def crawl4ai_extract_structured(url: str, extraction_schema: dict[str, str], use_cache: bool = True) -> dict:
    return get_crawl4ai_provider().extract_structured(url, extraction_schema, use_cache).model_dump()


def crawl4ai_extract_links(url: str, use_cache: bool = True) -> dict:
    return get_crawl4ai_provider().extract_links(url, use_cache).model_dump()


def crawl4ai_extract_metadata(url: str, use_cache: bool = True) -> dict:
    return get_crawl4ai_provider().extract_metadata(url, use_cache).model_dump()


_TOOLS: list[tuple[str, str, type[BaseModel], object]] = [
    ("crawl4ai_health_check", "Check whether Crawl4AI is installed and can crawl", HealthCheckArgs, crawl4ai_health_check),
    ("crawl4ai_crawl_url", "Crawl a single URL", CrawlUrlArgs, crawl4ai_crawl_url),
    ("crawl4ai_crawl_urls", "Crawl multiple URLs", CrawlUrlsArgs, crawl4ai_crawl_urls),
    ("crawl4ai_extract_markdown", "Extract a page's markdown", ExtractMarkdownArgs, crawl4ai_extract_markdown),
    ("crawl4ai_extract_structured", "Extract structured content via a field->selector schema", ExtractStructuredArgs, crawl4ai_extract_structured),
    ("crawl4ai_extract_links", "Extract a page's links", ExtractLinksArgs, crawl4ai_extract_links),
    ("crawl4ai_extract_metadata", "Extract a page's metadata", ExtractMetadataArgs, crawl4ai_extract_metadata),
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
