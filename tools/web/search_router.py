import logging
from functools import lru_cache
from pathlib import Path

import yaml

from tools.web.search_providers import (
    BraveSearchProvider,
    SearchProvider,
    SearchResult,
    SearxngProvider,
    TavilyProvider,
)

logger = logging.getLogger("afos.tools.search_router")

_ROUTING_FILE = Path(__file__).parent / "search_routing.yaml"

_PROVIDER_REGISTRY: dict[str, SearchProvider] = {
    "searxng": SearxngProvider(),
    "tavily": TavilyProvider(),
    "brave": BraveSearchProvider(),
}


class SearchRouter:
    """Tries each provider in the configured chain in order, falling through to the
    next on failure. No provider name is hardcoded into the Search Agent or the tool -
    both only ever call SearchRouter.search().
    """

    def __init__(self, chain: list[str], providers: dict[str, SearchProvider]) -> None:
        self._chain = chain
        self._providers = providers

    def search(self, query: str, max_results: int = 5, **kwargs: object) -> list[SearchResult]:
        last_exc: Exception | None = None
        for provider_name in self._chain:
            provider = self._providers.get(provider_name)
            if provider is None:
                logger.warning("search provider '%s' not registered, skipping", provider_name)
                continue
            try:
                return provider.search(query, max_results=max_results, **kwargs)
            except Exception as exc:
                last_exc = exc
                logger.warning("search provider '%s' failed: %s", provider_name, exc)
                continue
        raise RuntimeError(f"all search providers failed for query '{query}'") from last_exc


@lru_cache
def get_search_router() -> SearchRouter:
    with open(_ROUTING_FILE, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return SearchRouter(config["chain"], _PROVIDER_REGISTRY)
