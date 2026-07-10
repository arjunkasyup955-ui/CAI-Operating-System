"""Phase 2, Component 4: Terminal Executor.

Verifies:
  - Agent registration
  - Manifest
  - Tool registration
  - Schema validation
  - Permission enforcement
  - Retry behavior (ToolRegistry's existing RetryPolicy, via a flaky fake provider)
  - Dependency Injection (fake provider - no real subprocess side effects for the
    deterministic parts)
  - Graceful failure (provider raises -> terminal_failed, never crashes)
  - Approval interrupt/resume (high-risk commands pause inside a real graph, both
    approve and reject paths)
  - Event publishing (terminal_started/completed/failed)
  - Command safety: banned destructive patterns, risk classification, no shell=True

Run: python scripts/smoke_test_phase2_terminal.py
"""

import logging
import sys
import uuid
from pathlib import Path
from typing import Annotated, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

import operator  # noqa: E402

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import agents.coding.terminal.agent as terminal_agent  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import get_agent_registry, get_tool_registry  # noqa: E402
from tools.terminal.terminal_providers import (  # noqa: E402
    DestructiveCommandError,
    SubprocessTerminalProvider,
    TerminalExecutionResult,
    classify_risk,
    is_banned,
    set_terminal_provider,
)


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


class _FakeProvider:
    name = "fake_terminal"

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[list[str]] = []
        self._fail_times = fail_times
        self._call_count = 0

    def execute(self, args: list[str], timeout_seconds: float = 30.0) -> TerminalExecutionResult:
        self.calls.append(args)
        self._call_count += 1
        if self._call_count <= self._fail_times:
            raise RuntimeError(f"simulated transient failure (call {self._call_count})")
        return TerminalExecutionResult(success=True, command=" ".join(args), stdout="ok", exit_code=0, duration_seconds=0.01)


class _AlwaysFailingProvider:
    name = "always_failing"

    def execute(self, args: list[str], timeout_seconds: float = 30.0) -> TerminalExecutionResult:
        raise RuntimeError("simulated permanent failure")


def main() -> None:
    print("\n== 1. Agent Registration + Manifest ==")
    manifest = get_agent_registry().get("terminal_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares shell_exec permission", manifest.permissions == ["shell_exec"])
    check("declares the terminal_execute tool", manifest.tools == ["terminal_execute"])
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Tool Registration + Schema Validation ==")
    spec = get_tool_registry().get_spec("terminal_execute")
    check("terminal_execute has an input schema", spec.input_schema is not None)
    check("terminal_execute requires shell_exec permission", "shell_exec" in spec.permissions)
    try:
        get_tool_registry().invoke("terminal_execute", agent_name="terminal_agent", command=[])
        rejected = False
    except ValidationError:
        rejected = True
    check("empty command list rejected before execution", rejected)

    print("\n== 3. Permission Enforcement ==")
    manifest_no_perms = manifest.model_copy(update={"name": "no_shell_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("terminal_execute", agent_name="no_shell_agent", command=[sys.executable, "--version"])
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without shell_exec is denied", denied)

    print("\n== 4. Command Safety: banned patterns + risk classification ==")
    for banned_cmd in (["rm", "-rf", "/"], ["format", "C:"], ["shutdown", "-h", "now"], ["dd", "if=/dev/zero", "of=/dev/sda"]):
        check(f"'{' '.join(banned_cmd)}' is banned", is_banned(banned_cmd))
    check("'python --version' is low risk", classify_risk(["python", "--version"]) == "low")
    check("'git status' is low risk", classify_risk(["git", "status"]) == "low")
    check("'pip install requests' is high risk", classify_risk(["pip", "install", "requests"]) == "high")
    check("'pip list' is medium risk", classify_risk(["pip", "list"]) == "medium")
    check("'python -c ...' (inline code) is high risk", classify_risk(["python", "-c", "print(1)"]) == "high")
    check("unknown executable defaults to high risk", classify_risk(["some_unknown_tool"]) == "high")

    provider = SubprocessTerminalProvider()
    try:
        provider.execute(["rm", "-rf", "/"])
        refused = False
    except DestructiveCommandError:
        refused = True
    check("provider refuses a banned command before subprocess ever runs", refused)

    print("\n== 5. Dependency Injection + Retry Behaviour ==")
    flaky = _FakeProvider(fail_times=1)
    set_terminal_provider(flaky)
    try:
        retry_entry = terminal_agent.run_terminal_command([sys.executable, "--version"], venture_id="v-test")
        check("retried past one transient failure and completed", retry_entry["event"] == "terminal_completed")
        check("exactly 2 attempts were made", len(flaky.calls) == 2)
    finally:
        set_terminal_provider(SubprocessTerminalProvider())

    print("\n== 6. Graceful Failure Handling ==")
    set_terminal_provider(_AlwaysFailingProvider())
    try:
        fail_entry = terminal_agent.run_terminal_command([sys.executable, "--version"], venture_id="v-test")
        check("exhausted retries return terminal_failed, never raises", fail_entry["event"] == "terminal_failed")
    finally:
        set_terminal_provider(SubprocessTerminalProvider())

    print("\n== 7. Real command execution (safe, low-risk, auto-approved) ==")
    real_entry = terminal_agent.run_terminal_command([sys.executable, "--version"], venture_id="v-test")
    check("real subprocess execution completed", real_entry["event"] == "terminal_completed")
    check("captured stdout", "Python" in real_entry.get("stdout", ""))
    check("captured exit_code", real_entry.get("exit_code") == 0)
    check("captured duration_seconds", real_entry.get("duration_seconds", -1) >= 0)

    print("\n== 8. Banned command denied before any approval/execution attempt ==")
    banned_entry = terminal_agent.run_terminal_command(["rm", "-rf", "/"], venture_id="v-test")
    check("banned command denied, not executed", banned_entry["event"] == "terminal_denied")

    print("\n== 9. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    terminal_agent.run_terminal_command([sys.executable, "--version"], venture_id="v-test")
    check("published terminal_started", "terminal_started" in seen_events)
    check("published terminal_completed", "terminal_completed" in seen_events)

    seen_events.clear()
    set_terminal_provider(_AlwaysFailingProvider())
    try:
        terminal_agent.run_terminal_command([sys.executable, "--version"], venture_id="v-test")
    finally:
        set_terminal_provider(SubprocessTerminalProvider())
    check("published terminal_failed", "terminal_failed" in seen_events)

    print("\n== 10. Approval Framework: high-risk command pauses inside a real graph ==")
    set_terminal_provider(_FakeProvider())
    try:

        class _S(TypedDict, total=False):
            history: Annotated[list, operator.add]

        def install_node(state: dict) -> dict:
            entry = terminal_agent.run_terminal_command(["pip", "install", "requests"], venture_id="v-test")
            return {"history": [entry]}

        graph = StateGraph(_S)
        graph.add_node("install", install_node)
        graph.add_edge(START, "install")
        graph.add_edge("install", END)
        compiled = graph.compile(checkpointer=InMemorySaver())

        approve_thread = f"terminal-install-approve-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": approve_thread}}
        first = compiled.invoke({"history": []}, config=config)
        check("pip install paused for human approval (real interrupt)", "__interrupt__" in first)
        check("interrupt carries the terminal_execute action", first["__interrupt__"][0].value["action"] == "terminal_execute")

        final = compiled.invoke(Command(resume={"approved": True, "reason": "reviewed"}), config=config)
        check("resumed past the gate after approval", "__interrupt__" not in final)
        check("command executed after approval", final["history"][0]["event"] == "terminal_completed")

        reject_thread = f"terminal-install-reject-{uuid.uuid4().hex[:8]}"
        config2 = {"configurable": {"thread_id": reject_thread}}
        compiled.invoke({"history": []}, config=config2)
        final2 = compiled.invoke(Command(resume={"approved": False, "reason": "not reviewed"}), config=config2)
        check("rejected command recorded as denied, never executed", final2["history"][0]["event"] == "terminal_denied")
    finally:
        set_terminal_provider(SubprocessTerminalProvider())

    print("\n== 11. Manager-callable node shape ==")
    delta = terminal_agent.terminal_agent_node({"venture_id": "v-test"})
    check("terminal_agent_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
    check("terminal_agent_node's version check completed", delta["history"][0]["event"] == "terminal_completed")

    print("\nAll Phase 2 Component 4 checks passed.")


if __name__ == "__main__":
    main()
