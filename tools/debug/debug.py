from pydantic import BaseModel

from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.debug.debug_providers import get_debug_provider

_RETRY_POLICY = RetryPolicy(max_attempts=1, backoff_seconds=0.0)  # diagnosis is local/deterministic - no retry needed
_TIMEOUT_SECONDS = 5.0


class DiagnoseFailureArgs(BaseModel):
    operation: str
    error: str


def diagnose_failure(operation: str, error: str) -> dict:
    return get_debug_provider().diagnose(operation, error, {}).model_dump()


get_tool_registry().register(
    ToolSpec(
        name="diagnose_failure",
        description="Classify a failure as transient/permanent/unknown and produce a diagnosis + recommended fix",
        input_schema=DiagnoseFailureArgs,
        # No dedicated permission scope exists in the frozen kernel for "diagnostics".
        # This agent exists to analyze failures surfaced by the shell/git/file/build
        # agents, so it's granted the same baseline operational scope they use.
        permissions=["shell_exec"],
        retry_policy=_RETRY_POLICY,
        timeout_seconds=_TIMEOUT_SECONDS,
        cost_per_call_usd=0.0,
    ),
    diagnose_failure,
)
