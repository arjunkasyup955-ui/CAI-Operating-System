import os
from collections.abc import Iterator
from typing import Protocol

from dotenv import load_dotenv
from pydantic import BaseModel

# Deliberately self-contained (own env reading, not core.config.Settings) so this tool
# module never needs the frozen Phase 0 kernel to change to gain a new config key -
# same convention as tools/web/search_providers.py and tools/browser/browser_providers.py.
load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


class OllamaHealthStatus(BaseModel):
    healthy: bool
    base_url: str
    version: str = ""
    error: str = ""


class OllamaModelInfo(BaseModel):
    name: str
    size_bytes: int = 0
    modified_at: str = ""


class OllamaChatChunk(BaseModel):
    content: str
    done: bool = False


class OllamaOpsProvider(Protocol):
    """Operational capabilities for the local Ollama service - health, model
    discovery, streaming. Deliberately NOT a second chat-completion abstraction:
    core/model_router/providers.py already has an OllamaProvider (frozen Phase 0
    kernel) that handles ordinary chat via the Model Router's fallback chain. This
    protocol only covers what that kernel abstraction doesn't: health/models/streaming.
    """

    name: str

    def health_check(self) -> OllamaHealthStatus: ...
    def list_models(self) -> list[OllamaModelInfo]: ...
    def stream_chat(self, messages: list[dict], model: str, timeout_seconds: float = 60.0) -> Iterator[OllamaChatChunk]: ...


class RealOllamaOpsProvider:
    name = "ollama_ops"

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url or _env("OLLAMA_BASE_URL", "http://localhost:11434")

    def health_check(self) -> OllamaHealthStatus:
        import httpx

        try:
            response = httpx.get(f"{self._base_url}/api/version", timeout=5.0)
            response.raise_for_status()
            return OllamaHealthStatus(healthy=True, base_url=self._base_url, version=response.json().get("version", ""))
        except Exception as exc:
            return OllamaHealthStatus(healthy=False, base_url=self._base_url, error=str(exc))

    def list_models(self) -> list[OllamaModelInfo]:
        import httpx

        response = httpx.get(f"{self._base_url}/api/tags", timeout=10.0)
        response.raise_for_status()
        data = response.json()
        return [
            OllamaModelInfo(name=m.get("name", ""), size_bytes=m.get("size", 0), modified_at=m.get("modified_at", ""))
            for m in data.get("models", [])
        ]

    def stream_chat(self, messages: list[dict], model: str, timeout_seconds: float = 60.0) -> Iterator[OllamaChatChunk]:
        import httpx

        with httpx.stream(
            "POST",
            f"{self._base_url}/api/chat",
            json={"model": model, "messages": messages, "stream": True},
            timeout=timeout_seconds,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                import json as _json

                data = _json.loads(line)
                yield OllamaChatChunk(content=data.get("message", {}).get("content", ""), done=data.get("done", False))


_provider: OllamaOpsProvider = RealOllamaOpsProvider()


def get_ollama_ops_provider() -> OllamaOpsProvider:
    return _provider


def set_ollama_ops_provider(provider: OllamaOpsProvider) -> None:
    """Swappable, not cached - lets tests inject a deterministic fake provider."""
    global _provider
    _provider = provider
