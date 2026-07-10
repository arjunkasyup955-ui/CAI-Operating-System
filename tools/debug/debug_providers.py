from typing import Any, Literal, Protocol

from pydantic import BaseModel

# error substring -> human-readable explanation. Order matters only within a tier;
# transient is checked first so e.g. "connection refused" (transient) isn't ever
# mis-read as some permanent pattern.
_TRANSIENT_PATTERNS: dict[str, str] = {
    "timeout": "The operation exceeded its time limit - likely a slow network or overloaded service.",
    "timed out": "The operation exceeded its time limit - likely a slow network or overloaded service.",
    "connection refused": "The target service was not reachable - it may be temporarily down or not yet started.",
    "connection reset": "The connection was interrupted mid-operation - likely a transient network issue.",
    "temporarily unavailable": "The service reported it is temporarily unavailable.",
    "rate limit": "The request was throttled - the provider is enforcing a rate limit.",
    "too many requests": "The request was throttled - the provider is enforcing a rate limit.",
    "503": "The service returned a 503 Service Unavailable - likely a temporary outage.",
    "502": "The service returned a 502 Bad Gateway - likely a temporary upstream issue.",
    "504": "The service returned a 504 Gateway Timeout.",
    "no connection could be made": "The target service refused the connection - it may not be running.",
}

_PERMANENT_PATTERNS: dict[str, str] = {
    "permission denied": "The operation was denied due to insufficient permissions.",
    "not found": "The target resource does not exist.",
    "no such file": "The target file or path does not exist.",
    "does not exist": "The target resource does not exist.",
    "syntaxerror": "The command or code contains a syntax error.",
    "validationerror": "The input failed schema validation - the request itself is malformed.",
    "no module named": "A required package is not installed.",
    "not installed": "A required dependency is not installed.",
    "unauthorized": "The operation was rejected due to invalid or missing credentials.",
    "forbidden": "The operation was rejected - access is forbidden.",
    "already exists": "The target already exists - the operation cannot proceed as requested.",
    "refused to run a banned": "The command was refused by a hard safety rule - it will never be allowed to run.",
    "refused to touch": "The path was refused by a hard safety rule - it will never be allowed to be touched.",
}


class FailureClassification(BaseModel):
    failure_type: Literal["transient", "permanent", "unknown"]
    category: str
    diagnosis: str
    recommended_fix: str
    confidence: float = 0.0


class DebugProvider(Protocol):
    name: str

    def diagnose(self, operation: str, error: str, context: dict[str, Any]) -> FailureClassification: ...


class RuleBasedDebugProvider:
    """Default DebugProvider - deterministic keyword classification, not LLM-based.
    The transient/permanent decision gates whether the system retries automatically at
    all, so it must be fast and reliable rather than dependent on a possibly-slow or
    unavailable LLM call. A richer, LLM-backed provider could be swapped in later via
    the same DI seam without touching the retry orchestration logic.
    """

    name = "rule_based"

    def diagnose(self, operation: str, error: str, context: dict[str, Any]) -> FailureClassification:
        lowered = error.lower()

        for pattern, explanation in _TRANSIENT_PATTERNS.items():
            if pattern in lowered:
                return FailureClassification(
                    failure_type="transient",
                    category=pattern.replace(" ", "_"),
                    diagnosis=f"[{operation}] {explanation}",
                    recommended_fix="Retry automatically - this class of failure is typically resolved by waiting and trying again.",
                    confidence=0.8,
                )

        for pattern, explanation in _PERMANENT_PATTERNS.items():
            if pattern in lowered:
                return FailureClassification(
                    failure_type="permanent",
                    category=pattern.replace(" ", "_"),
                    diagnosis=f"[{operation}] {explanation}",
                    recommended_fix="Do not retry automatically - fix the underlying issue described in the diagnosis first.",
                    confidence=0.8,
                )

        # Deliberately conservative: an error we can't confidently classify is treated
        # as non-retryable rather than risking wasted/unsafe automatic retries.
        return FailureClassification(
            failure_type="permanent",
            category="unknown",
            diagnosis=f"[{operation}] Could not confidently classify this error: {error[:200]}",
            recommended_fix="Manual investigation required - treating conservatively as non-retryable.",
            confidence=0.3,
        )


_provider: DebugProvider = RuleBasedDebugProvider()


def get_debug_provider() -> DebugProvider:
    return _provider


def set_debug_provider(provider: DebugProvider) -> None:
    """Swappable, not cached - lets tests inject a fake/deterministic provider."""
    global _provider
    _provider = provider
