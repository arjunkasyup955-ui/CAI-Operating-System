import logging
from functools import lru_cache
from pathlib import Path

import yaml

from core.event_bus import AFOSEvent, get_event_bus
from core.model_router.providers import (
    ChatMessage,
    ChatProvider,
    GoogleProvider,
    ModelResponse,
    NvidiaProvider,
    OllamaProvider,
    OpenAIProvider,
)

logger = logging.getLogger("afos.model_router")

_ROUTING_FILE = Path(__file__).parent / "routing.yaml"


class ModelRouter:
    """Agents declare a capability ('high-reasoning', 'cheap-fast', ...), never a provider.
    The router resolves it via routing.yaml's fallback chain, trying each provider in order.
    """

    def __init__(self, routing_table: dict[str, list[dict[str, str]]], providers: dict[str, ChatProvider]) -> None:
        self._routing_table = routing_table
        self._providers = providers

    def chat(
        self,
        messages: list[ChatMessage],
        capability: str = "default",
        agent_name: str = "unknown",
        **kwargs: object,
    ) -> ModelResponse:
        chain = self._routing_table.get(capability, self._routing_table.get("default", []))
        if not chain:
            raise RuntimeError(f"no routing entries for capability '{capability}' and no default configured")

        last_exc: Exception | None = None
        for entry in chain:
            provider = self._providers.get(entry["provider"])
            if provider is None:
                logger.warning("provider '%s' not registered, skipping", entry["provider"])
                continue
            try:
                response = provider.chat(messages, model=entry["model"], **kwargs)
                get_event_bus().publish(
                    AFOSEvent(
                        type="llm.invoked",
                        source_agent=agent_name,
                        payload={
                            "provider": response.provider,
                            "model": response.model,
                            "cost_usd": response.cost_usd,
                            "input_tokens": response.input_tokens,
                            "output_tokens": response.output_tokens,
                        },
                    )
                )
                return response
            except Exception as exc:
                last_exc = exc
                logger.warning("provider '%s' failed for capability '%s': %s", entry["provider"], capability, exc)
                continue

        raise RuntimeError(f"all providers failed for capability '{capability}'") from last_exc


@lru_cache
def get_model_router() -> ModelRouter:
    with open(_ROUTING_FILE, encoding="utf-8") as f:
        routing_table = yaml.safe_load(f)
    providers: dict[str, ChatProvider] = {
        "google": GoogleProvider(),
        "openai": OpenAIProvider(),
        "ollama": OllamaProvider(),
        "nvidia": NvidiaProvider(),
    }
    return ModelRouter(routing_table, providers)
