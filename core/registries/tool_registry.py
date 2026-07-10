import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, ValidationError

from core.event_bus import AFOSEvent, get_event_bus
from core.permissions import PermissionDeniedError

logger = logging.getLogger("afos.tool_registry")


class RetryPolicy(BaseModel):
    max_attempts: int = 3
    backoff_seconds: float = 1.0


class RateLimit(BaseModel):
    max_calls: int = 60
    period_seconds: float = 60.0


class ToolSpec(BaseModel):
    """Every tool registers this once; the registry applies schema validation,
    permission enforcement, retry/timeout/rate-limit, logging, and cost-tracking
    uniformly so individual tools never reimplement any of it.
    """

    name: str
    description: str = ""
    input_schema: type[BaseModel] | None = None
    permissions: list[str] = []
    retry_policy: RetryPolicy = RetryPolicy()
    timeout_seconds: float = 30.0
    rate_limit: RateLimit = RateLimit()
    cost_per_call_usd: float = 0.0


class _RateLimiter:
    def __init__(self, rate_limit: RateLimit) -> None:
        self._rate_limit = rate_limit
        self._calls: list[float] = []

    def check(self) -> None:
        now = time.monotonic()
        window_start = now - self._rate_limit.period_seconds
        self._calls = [t for t in self._calls if t >= window_start]
        if len(self._calls) >= self._rate_limit.max_calls:
            raise RuntimeError(f"rate limit exceeded ({self._rate_limit.max_calls}/{self._rate_limit.period_seconds}s)")
        self._calls.append(now)


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._funcs: dict[str, Callable[..., Any]] = {}
        self._limiters: dict[str, _RateLimiter] = {}
        self._executor = ThreadPoolExecutor(max_workers=8)

    def register(self, spec: ToolSpec, func: Callable[..., Any]) -> None:
        self._specs[spec.name] = spec
        self._funcs[spec.name] = func
        self._limiters[spec.name] = _RateLimiter(spec.rate_limit)

    def get_spec(self, name: str) -> ToolSpec:
        return self._specs[name]

    def list_all(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def invoke(self, name: str, agent_name: str = "unknown", **kwargs: Any) -> Any:
        spec = self._specs[name]
        func = self._funcs[name]

        # Permission and schema checks are not retryable - they fail the call
        # immediately, before the tool ever executes once.
        self._enforce_permissions(spec, agent_name)
        kwargs = self._validate_schema(spec, kwargs)

        self._limiters[name].check()

        start = time.monotonic()
        last_exc: Exception | None = None
        for attempt in range(1, spec.retry_policy.max_attempts + 1):
            try:
                future = self._executor.submit(func, **kwargs)
                result = future.result(timeout=spec.timeout_seconds)
                duration_ms = (time.monotonic() - start) * 1000
                logger.info("tool '%s' ok in %.1fms (attempt %d)", name, duration_ms, attempt)
                self._publish_invocation(spec, agent_name, True, duration_ms)
                return result
            except FutureTimeoutError:
                last_exc = TimeoutError(f"tool '{name}' timed out after {spec.timeout_seconds}s")
                logger.warning("tool '%s' timed out (attempt %d)", name, attempt)
            except Exception as exc:
                last_exc = exc
                logger.warning("tool '%s' failed (attempt %d): %s", name, attempt, exc)
            if attempt < spec.retry_policy.max_attempts:
                time.sleep(spec.retry_policy.backoff_seconds * attempt)

        duration_ms = (time.monotonic() - start) * 1000
        self._publish_invocation(spec, agent_name, False, duration_ms)
        raise last_exc

    def _enforce_permissions(self, spec: ToolSpec, agent_name: str) -> None:
        """Deny-by-default: a tool that declares required permissions can only be
        invoked by an agent whose registered manifest grants every one of them.
        Not opt-in - every invoke() call goes through this.
        """
        if not spec.permissions:
            return

        from core.permissions import get_permission_gate
        from core.registries.agent_registry import get_agent_registry

        try:
            manifest = get_agent_registry().get(agent_name)
        except KeyError:
            raise PermissionDeniedError(
                f"tool '{spec.name}' requires permissions {spec.permissions}, but calling "
                f"agent '{agent_name}' is not a registered agent"
            ) from None

        gate = get_permission_gate()
        for permission in spec.permissions:
            gate.check(manifest, permission)

    def _validate_schema(self, spec: ToolSpec, kwargs: dict[str, Any]) -> dict[str, Any]:
        """If the tool declares a pydantic schema, validate (and coerce) kwargs
        against it before the tool ever runs. Raises pydantic.ValidationError on
        mismatch - never silently passes through malformed input.
        """
        if spec.input_schema is None:
            return kwargs
        try:
            validated = spec.input_schema(**kwargs)
        except ValidationError:
            logger.warning("tool '%s' rejected invalid input against its schema", spec.name)
            raise
        return validated.model_dump()

    def _publish_invocation(self, spec: ToolSpec, agent_name: str, success: bool, duration_ms: float) -> None:
        get_event_bus().publish(
            AFOSEvent(
                type="tool.invoked",
                source_agent=agent_name,
                payload={
                    "tool": spec.name,
                    "success": success,
                    "duration_ms": duration_ms,
                    "cost_usd": spec.cost_per_call_usd if success else 0.0,
                },
            )
        )


def register_tool(spec: ToolSpec) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        get_tool_registry().register(spec, func)
        return func

    return decorator


@lru_cache
def get_tool_registry() -> ToolRegistry:
    return ToolRegistry()
