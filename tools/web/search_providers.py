import os
from typing import Protocol

from dotenv import load_dotenv
from pydantic import BaseModel

# Deliberately self-contained (own env reading, not core.config.Settings) so this tool
# module never needs the frozen Phase 0 kernel to change to gain a new config key.
load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""
    provider: str


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, max_results: int = 5, **kwargs: object) -> list[SearchResult]: ...


class SearxngProvider:
    """Primary provider - a SearXNG metasearch instance. Self-hostable, no API key."""

    name = "searxng"

    def search(self, query: str, max_results: int = 5, **kwargs: object) -> list[SearchResult]:
        import httpx

        base_url = _env("SEARXNG_BASE_URL", "http://localhost:8080")
        response = httpx.get(
            f"{base_url}/search",
            params={"q": query, "format": "json"},
            timeout=kwargs.get("timeout", 15.0),
        )
        response.raise_for_status()
        data = response.json()
        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
                provider=self.name,
            )
            for item in data.get("results", [])[:max_results]
        ]


class TavilyProvider:
    """Fallback provider #1 - Tavily's search API (built for LLM agents)."""

    name = "tavily"

    def search(self, query: str, max_results: int = 5, **kwargs: object) -> list[SearchResult]:
        import httpx

        api_key = _env("TAVILY_API_KEY")
        if not api_key:
            raise RuntimeError("TAVILY_API_KEY is not configured")
        response = httpx.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "max_results": max_results},
            timeout=kwargs.get("timeout", 15.0),
        )
        response.raise_for_status()
        data = response.json()
        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
                provider=self.name,
            )
            for item in data.get("results", [])[:max_results]
        ]


class BraveSearchProvider:
    """Fallback provider #2 - Brave Search API."""

    name = "brave"

    def search(self, query: str, max_results: int = 5, **kwargs: object) -> list[SearchResult]:
        import httpx

        api_key = _env("BRAVE_API_KEY")
        if not api_key:
            raise RuntimeError("BRAVE_API_KEY is not configured")
        response = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max_results},
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
            timeout=kwargs.get("timeout", 15.0),
        )
        response.raise_for_status()
        data = response.json()
        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("description", ""),
                provider=self.name,
            )
            for item in data.get("web", {}).get("results", [])[:max_results]
        ]
