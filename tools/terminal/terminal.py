from pydantic import BaseModel, Field

from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.terminal.terminal_providers import get_terminal_provider

_RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_seconds=1.0)
_TOOL_TIMEOUT_SECONDS = 35.0


class TerminalExecuteArgs(BaseModel):
    # A list of literal argv tokens, never a shell string - structurally prevents
    # shell=True-style invocation at the schema level, not just in the provider.
    command: list[str] = Field(min_length=1)
    timeout_seconds: float = 30.0


def terminal_execute(command: list[str], timeout_seconds: float = 30.0) -> dict:
    return get_terminal_provider().execute(command, timeout_seconds=timeout_seconds).model_dump()


get_tool_registry().register(
    ToolSpec(
        name="terminal_execute",
        description="Execute a terminal command (argv list, never a shell string) and capture stdout/stderr/exit code/duration",
        input_schema=TerminalExecuteArgs,
        permissions=["shell_exec"],
        retry_policy=_RETRY_POLICY,
        timeout_seconds=_TOOL_TIMEOUT_SECONDS,
        cost_per_call_usd=0.0,
    ),
    terminal_execute,
)
