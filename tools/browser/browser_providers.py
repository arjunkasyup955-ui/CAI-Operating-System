import os
from typing import Protocol

from dotenv import load_dotenv
from pydantic import BaseModel

# Deliberately self-contained (own env reading, not core.config.Settings) so this tool
# module never needs the frozen Phase 0 kernel to change to gain a new config key -
# same convention as tools/web/search_providers.py.
load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


class FetchResult(BaseModel):
    url: str
    title: str = ""
    content: str = ""
    provider: str


class BrowserProvider(Protocol):
    name: str

    def fetch(self, url: str, **kwargs: object) -> FetchResult: ...


class PlaywrightProvider:
    """Primary provider - real headless-browser rendering via Playwright. Lazily
    imported so this module loads fine even when playwright isn't installed; a call
    then raises a clear, actionable error that BrowserRouter's fallback catches -
    installing playwright later requires no code change here.
    """

    name = "playwright"

    def fetch(self, url: str, **kwargs: object) -> FetchResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright is not installed - `pip install playwright` and `playwright install chromium`"
            ) from exc

        timeout_ms = kwargs.get("timeout_ms", 15000)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(url, timeout=timeout_ms)
                title = page.title()
                content = page.content()
            finally:
                browser.close()
        return FetchResult(url=url, title=title, content=content, provider=self.name)


class Crawl4AIProvider:
    """Fallback provider #1 - Crawl4AI, an LLM-oriented local crawler (itself built on
    a headless browser). Lazily imported for the same reason as PlaywrightProvider -
    not installed in this environment, but the adapter is real and complete.
    """

    name = "crawl4ai"

    def fetch(self, url: str, **kwargs: object) -> FetchResult:
        try:
            from crawl4ai import WebCrawler
        except ImportError as exc:
            raise RuntimeError("crawl4ai is not installed - `pip install crawl4ai`") from exc

        crawler = WebCrawler()
        crawler.warmup()
        result = crawler.run(url=url)
        return FetchResult(
            url=url,
            title=getattr(result, "title", "") or "",
            content=getattr(result, "markdown", "") or getattr(result, "html", "") or "",
            provider=self.name,
        )


class FirecrawlProvider:
    """Fallback provider #2 - Firecrawl's hosted scrape API. Plain REST + API key, no
    extra package needed, same pattern as TavilyProvider/BraveSearchProvider."""

    name = "firecrawl"

    def fetch(self, url: str, **kwargs: object) -> FetchResult:
        import httpx

        api_key = _env("FIRECRAWL_API_KEY")
        if not api_key:
            raise RuntimeError("FIRECRAWL_API_KEY is not configured")
        response = httpx.post(
            "https://api.firecrawl.dev/v1/scrape",
            json={"url": url},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=kwargs.get("timeout", 20.0),
        )
        response.raise_for_status()
        data = response.json().get("data", {})
        return FetchResult(
            url=url,
            title=data.get("metadata", {}).get("title", ""),
            content=data.get("markdown", "") or data.get("content", ""),
            provider=self.name,
        )
