import os
from typing import Any, Protocol

from dotenv import load_dotenv
from pydantic import BaseModel

# Deliberately self-contained (own env reading, not core.config.Settings) so this
# tool module never needs the frozen Phase 0 kernel to change to gain a new config
# key - same convention as tools/n8n, tools/playwright, tools/docker, tools/browser.
load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


class Crawl4AIHealthStatus(BaseModel):
    healthy: bool
    installed: bool
    error: str = ""


class Crawl4AIPage(BaseModel):
    url: str
    markdown: str = ""
    html: str = ""
    links: list[str] = []
    metadata: dict[str, Any] = {}
    structured: dict[str, Any] = {}
    cached: bool = False


class Crawl4AIOperationResult(BaseModel):
    success: bool
    operation: str
    pages: list[Crawl4AIPage] = []
    error: str = ""


class Crawl4AIProvider(Protocol):
    """Read-only crawling/extraction. Every method either returns a
    Crawl4AIOperationResult on success or raises on failure - the agent layer's
    try/except (with ToolRegistry's RetryPolicy) handles graceful degradation
    uniformly, the same pattern as every prior provider. Field names deliberately
    avoid "name" anywhere (ToolRegistry.invoke()'s own positional parameter is
    literally called `name` - a schema field called `name` collides with it; found
    and fixed in the Chroma component, Phase 3 Component 3, avoided proactively
    here since no field here is ever called "name").
    """

    name: str

    def health_check(self) -> Crawl4AIHealthStatus: ...
    def crawl_url(self, url: str, use_cache: bool = True) -> Crawl4AIOperationResult: ...
    def crawl_urls(self, urls: list[str], use_cache: bool = True) -> Crawl4AIOperationResult: ...
    def extract_markdown(self, url: str, use_cache: bool = True) -> Crawl4AIOperationResult: ...
    def extract_structured(self, url: str, extraction_schema: dict[str, str], use_cache: bool = True) -> Crawl4AIOperationResult: ...
    def extract_links(self, url: str, use_cache: bool = True) -> Crawl4AIOperationResult: ...
    def extract_metadata(self, url: str, use_cache: bool = True) -> Crawl4AIOperationResult: ...


class Crawl4AIRealProvider:
    """Default Crawl4AIProvider - real crawling via the `crawl4ai` package, lazily
    imported so this module loads fine even when crawl4ai isn't installed (confirmed
    not installed in this sandbox). A call then raises a clear, actionable error
    rather than crashing at import time - installing crawl4ai later requires no code
    change here. Uses the same sync `WebCrawler` entrypoint already established in
    tools/browser/browser_providers.py's Crawl4AIProvider, extended here with an
    in-memory per-URL cache so repeat calls with use_cache=True skip the crawl.
    """

    name = "crawl4ai_real"

    def __init__(self) -> None:
        self._cache: dict[str, Crawl4AIPage] = {}

    def health_check(self) -> Crawl4AIHealthStatus:
        try:
            from crawl4ai import WebCrawler
        except ImportError as exc:
            return Crawl4AIHealthStatus(healthy=False, installed=False, error=str(exc))

        try:
            crawler = WebCrawler()
            crawler.warmup()
            return Crawl4AIHealthStatus(healthy=True, installed=True)
        except Exception as exc:
            return Crawl4AIHealthStatus(healthy=False, installed=True, error=str(exc))

    def _fetch(self, url: str, use_cache: bool = True) -> Crawl4AIPage:
        if use_cache and url in self._cache:
            return self._cache[url].model_copy(update={"cached": True})

        try:
            from crawl4ai import WebCrawler
        except ImportError as exc:
            raise RuntimeError("crawl4ai is not installed - `pip install crawl4ai`") from exc

        crawler = WebCrawler()
        crawler.warmup()
        result = crawler.run(url=url)
        raw_links = getattr(result, "links", None) or {}
        if isinstance(raw_links, dict):
            links = list(raw_links.get("internal", [])) + list(raw_links.get("external", []))
        else:
            links = list(raw_links)
        page = Crawl4AIPage(
            url=url,
            markdown=getattr(result, "markdown", "") or "",
            html=getattr(result, "html", "") or "",
            links=links,
            metadata=getattr(result, "metadata", {}) or {},
            cached=False,
        )
        self._cache[url] = page
        return page

    def _extract_structured(self, page: Crawl4AIPage, extraction_schema: dict[str, str]) -> dict[str, Any]:
        # NOTE: crawl4ai's own schema-based extraction strategies (e.g.
        # JsonCssExtractionStrategy) are a distinct, version-sensitive API surface
        # not exercised here since no crawl4ai install exists in this sandbox. This
        # applies the caller-provided `schema` (field_name -> css_selector) via a
        # plain BeautifulSoup pass over the already-fetched HTML instead, so this
        # returns *something* structured without depending on crawl4ai's own
        # extraction-strategy classes; adjust if your crawl4ai version differs.
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return {field: None for field in extraction_schema}
        soup = BeautifulSoup(page.html, "html.parser")
        structured: dict[str, Any] = {}
        for field, selector in extraction_schema.items():
            el = soup.select_one(selector)
            structured[field] = el.get_text(strip=True) if el else None
        return structured

    def crawl_url(self, url: str, use_cache: bool = True) -> Crawl4AIOperationResult:
        return Crawl4AIOperationResult(success=True, operation="crawl_url", pages=[self._fetch(url, use_cache)])

    def crawl_urls(self, urls: list[str], use_cache: bool = True) -> Crawl4AIOperationResult:
        return Crawl4AIOperationResult(success=True, operation="crawl_urls", pages=[self._fetch(u, use_cache) for u in urls])

    def extract_markdown(self, url: str, use_cache: bool = True) -> Crawl4AIOperationResult:
        return Crawl4AIOperationResult(success=True, operation="extract_markdown", pages=[self._fetch(url, use_cache)])

    def extract_structured(self, url: str, extraction_schema: dict[str, str], use_cache: bool = True) -> Crawl4AIOperationResult:
        page = self._fetch(url, use_cache)
        page = page.model_copy(update={"structured": self._extract_structured(page, extraction_schema)})
        return Crawl4AIOperationResult(success=True, operation="extract_structured", pages=[page])

    def extract_links(self, url: str, use_cache: bool = True) -> Crawl4AIOperationResult:
        return Crawl4AIOperationResult(success=True, operation="extract_links", pages=[self._fetch(url, use_cache)])

    def extract_metadata(self, url: str, use_cache: bool = True) -> Crawl4AIOperationResult:
        return Crawl4AIOperationResult(success=True, operation="extract_metadata", pages=[self._fetch(url, use_cache)])


class FakeCrawl4AIProvider:
    """In-memory Crawl4AIProvider - deterministic, no real crawl needed. Maintains a
    genuine per-URL cache (not a stub): `fetch_count` only increments on an actual
    (non-cached) fetch, so the cache-support test exercises a real short-circuit
    rather than a canned response.
    """

    name = "fake_crawl4ai"

    def __init__(self) -> None:
        self._cache: dict[str, Crawl4AIPage] = {}
        self.fetch_count = 0

    def health_check(self) -> Crawl4AIHealthStatus:
        return Crawl4AIHealthStatus(healthy=True, installed=True)

    def _fetch(self, url: str, use_cache: bool = True) -> Crawl4AIPage:
        if use_cache and url in self._cache:
            return self._cache[url].model_copy(update={"cached": True})

        self.fetch_count += 1
        page = Crawl4AIPage(
            url=url,
            markdown=f"# Fake markdown for {url}",
            html=f"<html><head><title>Fake title for {url}</title></head><body>Fake body for {url}</body></html>",
            links=[f"{url}/link1", f"{url}/link2"],
            metadata={"title": f"Fake title for {url}", "description": f"Fake description for {url}"},
            cached=False,
        )
        self._cache[url] = page
        return page

    def crawl_url(self, url: str, use_cache: bool = True) -> Crawl4AIOperationResult:
        return Crawl4AIOperationResult(success=True, operation="crawl_url", pages=[self._fetch(url, use_cache)])

    def crawl_urls(self, urls: list[str], use_cache: bool = True) -> Crawl4AIOperationResult:
        return Crawl4AIOperationResult(success=True, operation="crawl_urls", pages=[self._fetch(u, use_cache) for u in urls])

    def extract_markdown(self, url: str, use_cache: bool = True) -> Crawl4AIOperationResult:
        return Crawl4AIOperationResult(success=True, operation="extract_markdown", pages=[self._fetch(url, use_cache)])

    def extract_structured(self, url: str, extraction_schema: dict[str, str], use_cache: bool = True) -> Crawl4AIOperationResult:
        page = self._fetch(url, use_cache)
        structured = {field: f"fake-value-for-{selector}" for field, selector in extraction_schema.items()}
        page = page.model_copy(update={"structured": structured})
        return Crawl4AIOperationResult(success=True, operation="extract_structured", pages=[page])

    def extract_links(self, url: str, use_cache: bool = True) -> Crawl4AIOperationResult:
        return Crawl4AIOperationResult(success=True, operation="extract_links", pages=[self._fetch(url, use_cache)])

    def extract_metadata(self, url: str, use_cache: bool = True) -> Crawl4AIOperationResult:
        return Crawl4AIOperationResult(success=True, operation="extract_metadata", pages=[self._fetch(url, use_cache)])


_provider: Crawl4AIProvider = Crawl4AIRealProvider()


def get_crawl4ai_provider() -> Crawl4AIProvider:
    return _provider


def set_crawl4ai_provider(provider: Crawl4AIProvider) -> None:
    """Swappable, not cached - lets tests inject a deterministic fake provider."""
    global _provider
    _provider = provider
