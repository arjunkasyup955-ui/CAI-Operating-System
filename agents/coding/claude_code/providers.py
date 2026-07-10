import json
import re
from typing import Any, Protocol

from pydantic import BaseModel, Field


class ClaudeCodeTaskRequest(BaseModel):
    task_description: str
    context: dict[str, Any] = Field(default_factory=dict)
    venture_id: str = ""


class ClaudeCodeTaskResult(BaseModel):
    success: bool
    summary: str = ""
    plan: list[str] = []
    output: str = ""
    # Always empty in this component - git, terminal execution, file editing, and MCP
    # are explicitly out of scope here and land as their own reviewed components.
    files_changed: list[str] = []
    commands_run: list[str] = []
    provider: str = ""


class ClaudeCodeProvider(Protocol):
    name: str

    def run_task(self, request: ClaudeCodeTaskRequest) -> ClaudeCodeTaskResult: ...


def _extract_json(text: str) -> str:
    """LLMs (especially 'thinking' models like qwen3:8b) may wrap the answer in
    reasoning text or markdown fences - pull out the first {...} block rather than
    assuming the whole response is a clean JSON document.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in model response")
    return match.group(0)


class ModelRouterClaudeCodeProvider:
    """Default ClaudeCodeProvider - delegates task *planning/reasoning* to the Model
    Router (a kernel capability, not a new tool). It never executes a shell command,
    touches git, edits a file, or calls an MCP tool: terminal execution, git
    integration, file editing, and MCP are explicitly out of scope for this component
    and will be added as their own, separately reviewed providers (e.g. a future
    ClaudeCodeCLIProvider that actually shells out to the `claude` binary).
    """

    name = "model_router_reasoning"

    def run_task(self, request: ClaudeCodeTaskRequest) -> ClaudeCodeTaskResult:
        from core.model_router import get_model_router

        prompt = (
            "You are Claude Code, a software engineering agent. You have been asked to "
            "PLAN (not execute) a software engineering task - you must not claim to "
            "have run any command, edited any file, or touched git. Produce a JSON "
            "object with exactly these keys: summary (string), plan (list of strings, "
            "ordered steps a human or a future execution-capable agent would follow). "
            "Respond with ONLY the JSON object, no other text.\n\n"
            f"Task: {request.task_description}\n\nContext: {request.context}"
        )
        response = get_model_router().chat(
            [{"role": "user", "content": prompt}],
            capability="high-reasoning",
            agent_name="claude_code_agent",
            timeout=120.0,
        )
        data = json.loads(_extract_json(response.content))
        return ClaudeCodeTaskResult(
            success=True,
            summary=data.get("summary", ""),
            plan=data.get("plan", []),
            output=response.content,
            files_changed=[],
            commands_run=[],
            provider=self.name,
        )
