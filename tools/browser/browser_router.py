import logging
from functools import lru_cache
from pathlib import Path

import yaml

from tools.browser.browser_providers import (
    BrowserProvider,
    Crawl4AIProvider,
    FetchResult,
    FirecrawlProvider,
    PlaywrightProvider,
)

logger = logging.getLogger("afos.tools.browser_router")

_ROUTING_FILE = Path(__file__).parent / "browser_routing.yaml"

_PROVIDER_REGISTRY: dict[str, BrowserProvider] = {
    "playwright": PlaywrightProvider(),
    "crawl4ai": Crawl4AIProvider(),
    "firecrawl": FirecrawlProvider(),
}


class BrowserRouter:
    """Tries each provider in the configured chain in order, falling through to the
    next on failure. No provider name is hardcoded into the Browser Agent or the tool -
    both only ever call BrowserRouter.fetch(). Mirrors tools/web/search_router.py.
    """

    def __init__(self, chain: list[str], providers: dict[str, BrowserProvider]) -> None:
        self._chain = chain
        self._providers = providers

    def fetch(self, url: str, **kwargs: object) -> FetchResult:
        last_exc: Exception | None = None
        for provider_name in self._chain:
            provider = self._providers.get(provider_name)
            if provider is None:
                logger.warning("browser provider '%s' not registered, skipping", provider_name)
                continue
            try:
                return provider.fetch(url, **kwargs)
            except Exception as exc:
                last_exc = exc
                logger.warning("browser provider '%s' failed: %s", provider_name, exc)
                continue
        raise RuntimeError(f"all browser providers failed for url '{url}'") from last_exc


@lru_cache
def get_browser_router() -> BrowserRouter:
    with open(_ROUTING_FILE, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return BrowserRouter(config["chain"], _PROVIDER_REGISTRY)
