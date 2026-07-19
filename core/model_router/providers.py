import logging
import threading
import time
from collections import deque
from typing import Protocol, TypedDict

from pydantic import BaseModel

from core.config import get_settings

logger = logging.getLogger("afos.model_router.providers")

# Rough per-1K-token USD pricing, only used for budget estimates - not billing-accurate.
_OPENAI_PRICING_PER_1K = {
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-4o": {"input": 0.005, "output": 0.015},
}

_GOOGLE_PRICING_PER_1K = {
    "gemini-flash-latest": {"input": 0.0003, "output": 0.0025},
    "gemini-pro-latest": {"input": 0.00125, "output": 0.01},
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


class GoogleProvider:
    """Gemini via Google's REST API directly (no google-generativeai SDK dependency needed)."""

    name = "google"

    def chat(self, messages: list[ChatMessage], model: str, **kwargs: object) -> ModelResponse:
        import httpx

        api_key = get_settings().google_api_key
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY is not configured")

        system_parts: list[str] = []
        contents = []
        for message in messages:
            if message["role"] == "system":
                system_parts.append(message["content"])
                continue
            role = "model" if message["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": message["content"]}]})

        body: dict[str, object] = {"contents": contents}
        if system_parts:
            body["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}

        response = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": api_key},
            json=body,
            timeout=kwargs.get("timeout", 60.0),
        )
        response.raise_for_status()
        data = response.json()
        content = data["candidates"][0]["content"]["parts"][0]["text"]
        usage = data.get("usageMetadata", {})
        input_tokens = usage.get("promptTokenCount", 0)
        output_tokens = usage.get("candidatesTokenCount", 0)
        pricing = _GOOGLE_PRICING_PER_1K.get(model, {"input": 0.0, "output": 0.0})
        cost_usd = (input_tokens / 1000) * pricing["input"] + (output_tokens / 1000) * pricing["output"]
        return ModelResponse(
            content=content,
            model=model,
            provider=self.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )


class NvidiaProvider:
    """NVIDIA NIM hosted inference (Nemotron family) via its OpenAI-compatible
    REST API - same self-contained httpx style as GoogleProvider/OllamaProvider,
    no new SDK dependency. Endpoint and auth verified standalone in
    scripts/test_nemotron_api.py before this integration.
    """

    name = "nvidia"

    # Process-wide counter (not per-instance) so its number reflects the true
    # total call volume across every agent/component that shares this one
    # ModelRouter/NvidiaProvider within a single run - the thing actually in
    # question (is AFOS's own redundant multi-invocation research pattern
    # what's exceeding NIM's 40 req/min budget, or is it leftover load from a
    # prior run). Resets naturally each fresh process invocation.
    _total_calls = 0

    # Proactive client-side rate limiting: a sliding window of call
    # timestamps, capped below NIM's free-tier 40 req/min ceiling. This is
    # deliberately separate from the reactive 429-backoff loop in chat()
    # below - that one recovers AFTER hitting the limit; this one sleeps
    # BEFORE a call would exceed it, so the limit is rarely if ever hit in
    # the first place. Process-wide (class-level) and lock-guarded, same
    # scope/rationale as _total_calls above.
    _MAX_CALLS_PER_MINUTE = 35  # buffer below NIM's 40 req/min free-tier limit
    _call_timestamps: deque = deque()
    _rate_limit_lock = threading.Lock()

    @classmethod
    def _throttle(cls) -> None:
        with cls._rate_limit_lock:
            now = time.monotonic()
            window = cls._call_timestamps
            while window and now - window[0] >= 60.0:
                window.popleft()
            if len(window) >= cls._MAX_CALLS_PER_MINUTE:
                sleep_for = 60.0 - (now - window[0]) + 0.05
                if sleep_for > 0:
                    logger.info(
                        "nvidia proactive throttle: %d calls in the last 60s (limit %d) - sleeping %.1fs before next call",
                        len(window), cls._MAX_CALLS_PER_MINUTE, sleep_for,
                    )
                    time.sleep(sleep_for)
                now = time.monotonic()
                while window and now - window[0] >= 60.0:
                    window.popleft()
            window.append(time.monotonic())

    def chat(self, messages: list[ChatMessage], model: str, **kwargs: object) -> ModelResponse:
        import random

        import httpx

        api_key = get_settings().nvidia_api_key
        if not api_key:
            raise RuntimeError("NVIDIA_API_KEY is not configured")

        body: dict[str, object] = {"model": model, "messages": messages, "max_tokens": kwargs.get("max_tokens", 1024)}
        if kwargs.get("json_mode"):
            # Verified empirically against the hosted NIM endpoint: this makes
            # Nemotron return pure JSON with no markdown fences and no
            # surrounding reasoning text, eliminating the failure mode at its
            # source rather than parsing around it after the fact. Only added
            # when the caller opts in (agents that already prompt for JSON) -
            # other providers in the same fallback chain silently ignore this
            # kwarg, so it's safe to pass through ModelRouter unconditionally.
            body["response_format"] = {"type": "json_object"}

        max_attempts = 4  # 1 initial attempt + up to 3 retries, per spec
        base_delay_seconds = 2.0  # -> 2s, 4s, 8s across the 3 retries
        response: httpx.Response | None = None
        for attempt in range(1, max_attempts + 1):
            NvidiaProvider._throttle()
            NvidiaProvider._total_calls += 1
            call_number = NvidiaProvider._total_calls
            logger.info("nvidia call #%d starting (attempt %d/%d, model=%s)", call_number, attempt, max_attempts, model)
            response = httpx.post(
                "https://integrate.api.nvidia.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
                timeout=kwargs.get("timeout", 60.0),
            )
            if response.status_code >= 400:
                # Diagnostic only - captures NIM's actual error body (not just
                # the generic httpx status line raise_for_status() would give)
                # for ANY non-2xx response, not just 429, so the next
                # occurrence of an unexplained error (e.g. the 400s seen in
                # testing) is self-documenting instead of needing a live
                # reproduction attempt after the fact.
                logger.warning(
                    "nvidia call #%d got HTTP %d (attempt %d/%d) - response body: %s",
                    call_number, response.status_code, attempt, max_attempts, response.text[:500],
                )
            if response.status_code != 429 or attempt == max_attempts:
                break

            retry_after_header = response.headers.get("Retry-After") or response.headers.get("retry-after")
            if retry_after_header:
                try:
                    delay = float(retry_after_header)
                except ValueError:
                    delay = base_delay_seconds * (2 ** (attempt - 1))
            else:
                delay = base_delay_seconds * (2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.5)  # jitter, up to +50%, to avoid synchronized retry storms
            logger.warning(
                "nvidia call #%d hit 429 (attempt %d/%d) - backing off %.1fs before retrying (%s)",
                call_number, attempt, max_attempts, delay, "Retry-After header" if retry_after_header else "exponential backoff",
            )
            time.sleep(delay)

        assert response is not None
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage", {})
        return ModelResponse(
            content=content,
            model=model,
            provider=self.name,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            cost_usd=0.0,  # NVIDIA NIM hosted free-tier - no published per-token billing
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
