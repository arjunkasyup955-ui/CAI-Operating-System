import os
from typing import Protocol

from dotenv import load_dotenv
from pydantic import BaseModel

# Deliberately self-contained (own env reading, not core.config.Settings) so this
# tool module never needs the frozen Phase 0 kernel to change to gain a new config
# key - same convention as tools/n8n, tools/playwright, tools/crawl4ai, tools/web.
load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


class SearXNGHealthStatus(BaseModel):
    healthy: bool
    base_url: str
    error: str = ""


class SearXNGResult(BaseModel):
    title: str
    url: str
    content: str = ""
    category: str = ""
    engine: str = ""
    thumbnail: str = ""


class SearXNGOperationResult(BaseModel):
    success: bool
    operation: str
    query: str = ""
    results: list[SearXNGResult] = []
    number_of_results: int = 0
    error: str = ""


class SearXNGProvider(Protocol):
    """Read-only metasearch. A single parameterized `search` method backs all three
    tool-level operations (web/news/image search) - category, language, safe_search,
    and max_results are just arguments, not separate provider methods. Every method
    either returns a SearXNGOperationResult on success or raises on failure - the
    agent layer's try/except (with ToolRegistry's RetryPolicy) handles graceful
    degradation uniformly, the same pattern as every prior provider.
    """

    name: str

    def health_check(self) -> SearXNGHealthStatus: ...
    def search(
        self,
        query: str,
        category: str = "general",
        language: str = "all",
        safe_search: int = 1,
        max_results: int = 10,
    ) -> SearXNGOperationResult: ...


class SearXNGRESTProvider:
    """Default SearXNGProvider - real integration via a SearXNG instance's JSON
    search API, over httpx (already an AFOS dependency - no new package needed, same
    reasoning as tools/n8n). The HTTP client itself is lazily initialized on first
    use ("lazy initialization"), and every call gracefully surfaces connection
    failures rather than crashing, exactly the same defensive posture as every
    other real provider in this project.
    """

    name = "searxng_rest"

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url or _env("SEARXNG_BASE_URL", "http://localhost:8080")
        self._client = None

    def _get_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(base_url=self._base_url, timeout=10.0)
        return self._client

    def health_check(self) -> SearXNGHealthStatus:
        # NOTE: not every SearXNG deployment exposes a dedicated /healthz endpoint
        # (it was added in some but not all instances/versions) - unverified against
        # a live server in this sandbox (none is running). Adjust the path if your
        # instance differs; any non-2xx/connection failure is still reported
        # gracefully via `healthy=False` rather than raising.
        try:
            response = self._get_client().get("/healthz", timeout=5.0)
            response.raise_for_status()
            return SearXNGHealthStatus(healthy=True, base_url=self._base_url)
        except Exception as exc:
            return SearXNGHealthStatus(healthy=False, base_url=self._base_url, error=str(exc))

    def search(
        self,
        query: str,
        category: str = "general",
        language: str = "all",
        safe_search: int = 1,
        max_results: int = 10,
    ) -> SearXNGOperationResult:
        response = self._get_client().get(
            "/search",
            params={
                "q": query,
                "format": "json",
                "categories": category,
                "language": language,
                "safesearch": safe_search,
                "pageno": 1,
            },
        )
        response.raise_for_status()
        data = response.json()
        raw_results = data.get("results", [])
        results = [
            SearXNGResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                content=r.get("content", ""),
                category=r.get("category", category),
                engine=r.get("engine", ""),
                thumbnail=r.get("thumbnail", "") or r.get("img_src", ""),
            )
            for r in raw_results[:max_results]
        ]
        return SearXNGOperationResult(
            success=True, operation="search", query=query, results=results, number_of_results=len(raw_results),
        )


class FakeSearXNGProvider:
    """In-memory SearXNGProvider - deterministic, no real SearXNG instance needed.
    Records every call's effective arguments (`self.calls`) so tests can assert
    category/language/safe_search/max_results were genuinely threaded through, not
    just accepted and ignored.
    """

    name = "fake_searxng"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def health_check(self) -> SearXNGHealthStatus:
        return SearXNGHealthStatus(healthy=True, base_url="fake")

    def search(
        self,
        query: str,
        category: str = "general",
        language: str = "all",
        safe_search: int = 1,
        max_results: int = 10,
    ) -> SearXNGOperationResult:
        self.calls.append(
            {"query": query, "category": category, "language": language, "safe_search": safe_search, "max_results": max_results}
        )
        all_results = [
            SearXNGResult(
                title=f"{category} result {i} for {query}",
                url=f"https://example.com/{category}/{i}",
                content=f"fake {category} snippet {i} ({language})",
                category=category,
                engine="fake_engine",
                thumbnail=f"https://example.com/{category}/{i}/thumb.png" if category == "images" else "",
            )
            for i in range(1, 21)
        ]
        limited = all_results[:max_results]
        return SearXNGOperationResult(
            success=True, operation="search", query=query, results=limited, number_of_results=len(all_results),
        )


_provider: SearXNGProvider = SearXNGRESTProvider()


def get_searxng_provider() -> SearXNGProvider:
    return _provider


def set_searxng_provider(provider: SearXNGProvider) -> None:
    """Swappable, not cached - lets tests inject a deterministic fake provider."""
    global _provider
    _provider = provider
