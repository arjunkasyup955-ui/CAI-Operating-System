import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp

import tools.debug.debug  # noqa: F401  (import registers the diagnose_failure tool)
from core.approval import ApprovalRequest, get_approval_engine
from core.event_bus import AFOSEvent, get_event_bus
from core.registries import get_agent_registry, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from core.state import VentureState

logger = logging.getLogger("afos.agents.debug_retry")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

# Reuses the exact kernel RetryPolicy shape ToolRegistry uses (core/registries/
# tool_registry.py) for configuration (max_attempts, backoff_seconds); the delay
# itself is computed with genuine exponential growth below, per this component's
# explicit "exponential backoff" requirement (the shared kernel loop is linear).
_DEFAULT_RETRY_POLICY = RetryPolicy(max_attempts=3, backoff_seconds=0.5)


def _looks_failed(entry: dict[str, Any]) -> bool:
    return str(entry.get("event", "")).endswith(("_failed", "_denied"))


def run_debug_retry(
    operation: str,
    retry_fn: Callable[[], dict],
    initial_error: str,
    venture_id: str = "default",
    retry_policy: RetryPolicy = _DEFAULT_RETRY_POLICY,
) -> dict:
    """Core logic. Always goes through, in order: Event Bus (started), Tool Registry
    (diagnose_failure - schema validation + permission enforcement), and then either:
      - failure_type != transient -> report diagnosis immediately, zero retries
      - failure_type == transient -> Approval Framework (low risk - retrying a
        transient failure is inherently low-stakes/reversible) -> automatic retry
        loop with real exponential backoff, bounded by retry_policy.max_attempts
    Never raises for a genuine error - always returns a well-formed entry dict.

    Important: the Approval Framework's high-risk path calls LangGraph's interrupt(),
    which raises GraphInterrupt (a subclass of Exception!) to pause the graph. A
    blanket `except Exception` would silently swallow that pause and break the gate
    entirely - GraphBubbleUp (its parent class) must always be re-raised, never
    treated as a failure. (Same fix already applied in the Git, File Editor, Terminal,
    and Build Runner Agents.)
    """
    bus = get_event_bus()

    bus.publish(
        AFOSEvent(
            type="debug_started",
            source_agent="debug_retry_agent",
            venture_id=venture_id,
            payload={"operation": operation, "error": initial_error},
        )
    )

    try:
        classification = get_tool_registry().invoke(
            "diagnose_failure", agent_name="debug_retry_agent", operation=operation, error=initial_error
        )

        base_entry = {
            "agent": "debug_retry_agent",
            "operation": operation,
            "failure_type": classification["failure_type"],
            "category": classification["category"],
            "diagnosis": classification["diagnosis"],
            "recommended_fix": classification["recommended_fix"],
            "confidence": classification["confidence"],
        }

        if classification["failure_type"] != "transient":
            entry = {**base_entry, "event": "debug_completed", "retried": False, "retry_attempts": 0}
            bus.publish(
                AFOSEvent(type="debug_completed", source_agent="debug_retry_agent", venture_id=venture_id, payload=entry)
            )
            return entry

        # Transient - retrying is inherently low-stakes (the whole premise of
        # "transient" is that trying again is safe), so this auto-approves under the
        # default policy while still genuinely exercising the Approval Framework.
        decision = get_approval_engine().request(
            ApprovalRequest(
                action=f"debug_retry:{operation}", venture_id=venture_id, risk_level="low", details={"category": classification["category"]}
            )
        )
        if not decision.approved:
            entry = {**base_entry, "event": "debug_completed", "retried": False, "retry_attempts": 0, "retry_denied_reason": decision.reason}
            bus.publish(
                AFOSEvent(type="debug_completed", source_agent="debug_retry_agent", venture_id=venture_id, payload=entry)
            )
            return entry

        bus.publish(
            AFOSEvent(type="retry_started", source_agent="debug_retry_agent", venture_id=venture_id, payload={"operation": operation})
        )

        last_result: dict[str, Any] = {}
        for attempt in range(1, retry_policy.max_attempts + 1):
            try:
                last_result = retry_fn()
                if not _looks_failed(last_result):
                    entry = {
                        **base_entry,
                        "event": "debug_completed",
                        "retried": True,
                        "retry_attempts": attempt,
                        "retry_budget_exhausted": False,
                        "retry_result": last_result,
                    }
                    bus.publish(
                        AFOSEvent(
                            type="retry_completed",
                            source_agent="debug_retry_agent",
                            venture_id=venture_id,
                            payload={"operation": operation, "succeeded": True, "attempts": attempt},
                        )
                    )
                    bus.publish(
                        AFOSEvent(type="debug_completed", source_agent="debug_retry_agent", venture_id=venture_id, payload=entry)
                    )
                    return entry
            except Exception as retry_exc:
                last_result = {"event": "retry_exception", "error": str(retry_exc)}

            if attempt < retry_policy.max_attempts:
                delay = retry_policy.backoff_seconds * (2 ** (attempt - 1))
                time.sleep(delay)

        # Retry budget exhausted - stop here, never retries endlessly.
        entry = {
            **base_entry,
            "event": "debug_completed",
            "retried": True,
            "retry_attempts": retry_policy.max_attempts,
            "retry_budget_exhausted": True,
            "last_retry_result": last_result,
        }
        bus.publish(
            AFOSEvent(
                type="retry_completed",
                source_agent="debug_retry_agent",
                venture_id=venture_id,
                payload={"operation": operation, "succeeded": False, "attempts": retry_policy.max_attempts},
            )
        )
        bus.publish(AFOSEvent(type="debug_completed", source_agent="debug_retry_agent", venture_id=venture_id, payload=entry))
        return entry
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("debug_retry_agent: operation '%s' failed: %s", operation, exc)
        entry = {"agent": "debug_retry_agent", "event": "debug_failed", "operation": operation, "error": str(exc)}
        bus.publish(AFOSEvent(type="debug_failed", source_agent="debug_retry_agent", venture_id=venture_id, payload=entry))
        return entry


def debug_retry_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Uses a trivial always-succeeding
    retry_fn as a placeholder - real usage passes a callable that re-invokes whatever
    other agent's operation actually failed.
    """
    entry = run_debug_retry(
        operation="placeholder_check",
        retry_fn=lambda: {"event": "noop_completed"},
        initial_error="connection timeout while checking placeholder",
        venture_id=state.get("venture_id", "default"),
    )
    return {"history": [entry]}
