import logging
import time
from pathlib import Path
from typing import Any

from agents.coding.claude_code.providers import (
    ClaudeCodeProvider,
    ClaudeCodeTaskRequest,
    ModelRouterClaudeCodeProvider,
)
from core.event_bus import AFOSEvent, get_event_bus
from core.registries import get_agent_registry
from core.registries.tool_registry import RetryPolicy
from core.state import VentureState

logger = logging.getLogger("afos.agents.claude_code")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

# Reuses the exact kernel RetryPolicy shape ToolRegistry uses (core/registries/
# tool_registry.py) rather than inventing a parallel retry config - "existing kernel
# retry policies", not a new one.
_DEFAULT_RETRY_POLICY = RetryPolicy(max_attempts=3, backoff_seconds=1.0)


def run_claude_code_delegation(
    request: ClaudeCodeTaskRequest,
    provider: ClaudeCodeProvider | None = None,
    retry_policy: RetryPolicy = _DEFAULT_RETRY_POLICY,
) -> dict[str, Any]:
    """Core logic, factored out from claude_code_node so tests can inject a
    deterministic fake provider instead of exercising a live, slow, non-deterministic
    LLM call - same dependency-injection pattern as every Research Supervisor
    analysis worker. Never raises: always returns a well-formed entry dict.
    """
    provider = provider or ModelRouterClaudeCodeProvider()
    bus = get_event_bus()

    if not request.task_description.strip():
        entry: dict[str, Any] = {
            "agent": "claude_code_agent",
            "event": "claude_code_skipped",
            "reason": "empty task description - nothing to delegate",
        }
        bus.publish(AFOSEvent(type="claude_code_skipped", source_agent="claude_code_agent", payload=entry))
        return entry

    bus.publish(
        AFOSEvent(
            type="claude_code_started",
            source_agent="claude_code_agent",
            venture_id=request.venture_id or None,
            payload={"task_description": request.task_description},
        )
    )

    last_exc: Exception | None = None
    for attempt in range(1, retry_policy.max_attempts + 1):
        try:
            result = provider.run_task(request)
            entry = {"agent": "claude_code_agent", "event": "claude_code_completed", **result.model_dump()}
            bus.publish(
                AFOSEvent(
                    type="claude_code_completed",
                    source_agent="claude_code_agent",
                    venture_id=request.venture_id or None,
                    payload={"provider": result.provider, "summary": result.summary},
                )
            )
            return entry
        except Exception as exc:
            last_exc = exc
            logger.warning("claude_code_agent: provider failed (attempt %d): %s", attempt, exc)
            if attempt < retry_policy.max_attempts:
                time.sleep(retry_policy.backoff_seconds * attempt)

    entry = {"agent": "claude_code_agent", "event": "claude_code_failed", "error": str(last_exc)}
    bus.publish(
        AFOSEvent(
            type="claude_code_failed",
            source_agent="claude_code_agent",
            venture_id=request.venture_id or None,
            payload=entry,
        )
    )
    return entry


def claude_code_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses,
    for future graph wiring. Not wired into any graph in this component - only the
    Manager delegating a task, and this function being able to serve that delegation,
    is in scope here. Derives a task_description from the venture idea as a
    placeholder mapping; real task derivation logic is a future integration concern.
    """
    request = ClaudeCodeTaskRequest(
        task_description=state.get("idea", ""),
        venture_id=state.get("venture_id", ""),
    )
    entry = run_claude_code_delegation(request)
    return {"history": [entry]}
