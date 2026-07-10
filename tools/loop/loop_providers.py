from typing import Any, Protocol

from pydantic import BaseModel


class LoopStepResult(BaseModel):
    success: bool
    step_index: int
    output: str = ""
    error: str = ""


class LoopProvider(Protocol):
    name: str

    def execute_step(self, step: dict[str, Any], step_index: int) -> LoopStepResult: ...


class EchoLoopProvider:
    """Default LoopProvider - executes no real external action, just simulates
    completion of whatever step descriptor it's given. This component's job is the
    pause/resume/cancel/retry/max-iteration/timeout control flow around a sequence of
    steps, not any specific step's business logic - real work would be wired in via a
    custom provider (e.g. one that dispatches each step to the Git/File Editor/
    Terminal/Build Runner Agent), swapped in through the same DI seam.
    """

    name = "echo"

    def execute_step(self, step: dict[str, Any], step_index: int) -> LoopStepResult:
        return LoopStepResult(success=True, step_index=step_index, output=f"executed step {step_index}: {step}")


_provider: LoopProvider = EchoLoopProvider()


def get_loop_provider() -> LoopProvider:
    return _provider


def set_loop_provider(provider: LoopProvider) -> None:
    """Swappable, not cached - lets tests inject a deterministic fake provider."""
    global _provider
    _provider = provider
