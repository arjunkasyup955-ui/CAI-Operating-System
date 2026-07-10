from typing import Protocol, TypedDict

from pydantic import BaseModel

from core.config import get_settings

# Rough per-1K-token USD pricing, only used for budget estimates - not billing-accurate.
_OPENAI_PRICING_PER_1K = {
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-4o": {"input": 0.005, "output": 0.015},
}


class ChatMessage(TypedDict):
    role: str
    content: str


class ModelResponse(BaseModel):
    content: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class ChatProvider(Protocol):
    name: str

    def chat(self, messages: list[ChatMessage], model: str, **kwargs: object) -> ModelResponse: ...


class OpenAIProvider:
    """Wraps the openai SDK directly (no langchain-openai dependency needed for Phase 0)."""

    name = "openai"

    def __init__(self) -> None:
        self._client = None  # lazy: don't require an API key just to import this module

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=get_settings().openai_api_key)
        return self._client

    def chat(self, messages: list[ChatMessage], model: str, **kwargs: object) -> ModelResponse:
        client = self._get_client()
        completion = client.chat.completions.create(model=model, messages=messages, **kwargs)
        usage = completion.usage
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0
        pricing = _OPENAI_PRICING_PER_1K.get(model, {"input": 0.0, "output": 0.0})
        cost_usd = (input_tokens / 1000) * pricing["input"] + (output_tokens / 1000) * pricing["output"]
        return ModelResponse(
            content=completion.choices[0].message.content or "",
            model=model,
            provider=self.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )


class OllamaProvider:
    """Local models via Ollama's REST API over httpx - no SDK dependency needed."""

    name = "ollama"

    def chat(self, messages: list[ChatMessage], model: str, **kwargs: object) -> ModelResponse:
        import httpx

        base_url = get_settings().ollama_base_url
        response = httpx.post(
            f"{base_url}/api/chat",
            json={"model": model, "messages": messages, "stream": False},
            timeout=kwargs.get("timeout", 60.0),
        )
        response.raise_for_status()
        data = response.json()
        return ModelResponse(
            content=data.get("message", {}).get("content", ""),
            model=model,
            provider=self.name,
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
            cost_usd=0.0,
        )
