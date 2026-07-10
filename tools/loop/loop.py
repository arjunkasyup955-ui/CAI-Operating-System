from typing import Any

from pydantic import BaseModel

from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.loop.loop_providers import get_loop_provider

# max_attempts=1: the loop's own retry_budget (checked in the agent's step node)
# governs retries at the step level - a second retry layer here would double-retry.
_RETRY_POLICY = RetryPolicy(max_attempts=1, backoff_seconds=0.0)
_TIMEOUT_SECONDS = 30.0


class ExecuteLoopStepArgs(BaseModel):
    step: dict[str, Any]
    step_index: int


def execute_loop_step(step: dict[str, Any], step_index: int) -> dict:
    return get_loop_provider().execute_step(step, step_index).model_dump()


get_tool_registry().register(
    ToolSpec(
        name="execute_loop_step",
        description="Execute a single step of a long-running loop via the configured LoopProvider",
        input_schema=ExecuteLoopStepArgs,
        permissions=["shell_exec"],
        retry_policy=_RETRY_POLICY,
        timeout_seconds=_TIMEOUT_SECONDS,
        cost_per_call_usd=0.0,
    ),
    execute_loop_step,
)
