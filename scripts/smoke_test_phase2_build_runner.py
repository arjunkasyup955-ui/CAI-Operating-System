"""Phase 2, Component 5: Build Runner.

Verifies:
  - Agent registration
  - Manifest
  - Tool registration
  - Schema validation
  - Permission enforcement
  - Retry behavior (ToolRegistry's existing RetryPolicy, via a flaky fake provider)
  - Dependency Injection (fake provider)
  - Approval interrupt/resume (install, high risk, pauses inside a real graph, both
    approve and reject paths)
  - Graceful failure (provider raises -> build_failed, never crashes)
  - Event publishing (build_started/completed/failed)
  - Regression tests (run separately, see the full suite this script is part of)

Run: python scripts/smoke_test_phase2_build_runner.py
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

import agents.coding.build_runner.agent as build_runner  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import get_agent_registry, get_tool_registry  # noqa: E402
from tools.build.build_providers import (  # noqa: E402
    BuildResult,
    SubprocessBuildProvider,
    classify_build_risk,
    set_build_provider,
)
from tools.terminal.terminal_providers import DestructiveCommandError, is_banned  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


class _FakeProvider:
    name = "fake_build"

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self._fail_times = fail_times
        self._call_count = 0

    def run(self, build_type: str, args: list[str], timeout_seconds: float = 120.0) -> BuildResult:
        self.calls.append((build_type, args))
        self._call_count += 1
        if self._call_count <= self._fail_times:
            raise RuntimeError(f"simulated transient failure (call {self._call_count})")
        return BuildResult(success=True, build_type=build_type, command=" ".join(args), stdout="ok", exit_code=0, status="passed")


class _AlwaysFailingProvider:
    name = "always_failing"

    def run(self, build_type: str, args: list[str], timeout_seconds: float = 120.0) -> BuildResult:
        raise RuntimeError("simulated permanent failure")


def main() -> None:
    print("\n== 1. Agent Registration + Manifest ==")
    manifest = get_agent_registry().get("build_runner_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares shell_exec permission", manifest.permissions == ["shell_exec"])
    check("declares all 3 tools", set(manifest.tools) == {"build_python", "build_node", "build_generic"})
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Tool Registration + Schema Validation ==")
    spec = get_tool_registry().get_spec("build_python")
    check("build_python has an input schema", spec.input_schema is not None)
    check("build_python requires shell_exec permission", "shell_exec" in spec.permissions)
    try:
        get_tool_registry().invoke("build_python", agent_name="build_runner_agent", action="not_a_real_action")
        rejected = False
    except ValidationError:
        rejected = True
    check("invalid action literal rejected before execution", rejected)

    print("\n== 3. Permission Enforcement ==")
    manifest_no_perms = manifest.model_copy(update={"name": "no_build_perms_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("build_python", agent_name="no_build_perms_agent", action="test")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without shell_exec is denied", denied)

    print("\n== 4. Risk Classification + Banned Command Refusal ==")
    check("'install' action is high risk", classify_build_risk("install", ["pip", "install", "-e", "."]) == "high")
    check("'test' action is low risk", classify_build_risk("test", ["pytest"]) == "low")
    check("'build' action is medium risk", classify_build_risk("build", ["npm", "run", "build"]) == "medium")
    check("'publish' keyword forces high risk", classify_build_risk("build", ["npm", "publish"]) == "high")
    check("unknown action defaults to high risk", classify_build_risk("deploy", ["some", "deploy", "cmd"]) == "high")
    check("banned pattern detected for generic builds", is_banned(["rm", "-rf", "/"]))

    provider = SubprocessBuildProvider()
    try:
        provider.run("generic", ["rm", "-rf", "/"])
        refused = False
    except DestructiveCommandError:
        refused = True
    check("provider refuses a banned build command before subprocess ever runs", refused)

    print("\n== 5. Dependency Injection + Retry Behaviour ==")
    flaky = _FakeProvider(fail_times=1)
    set_build_provider(flaky)
    try:
        retry_entry = build_runner.run_build_operation("python", venture_id="v-test", action="test")
        check("retried past one transient failure and completed", retry_entry["event"] == "build_completed")
        check("exactly 2 attempts were made", len(flaky.calls) == 2)
    finally:
        set_build_provider(SubprocessBuildProvider())

    print("\n== 6. Graceful Failure Handling ==")
    set_build_provider(_AlwaysFailingProvider())
    try:
        fail_entry = build_runner.run_build_operation("python", venture_id="v-test", action="test")
        check("exhausted retries return build_failed, never raises", fail_entry["event"] == "build_failed")
    finally:
        set_build_provider(SubprocessBuildProvider())

    print("\n== 7. Real command execution (low risk, auto-approved, accurate capture) ==")
    real_entry = build_runner.run_build_operation("python", venture_id="v-test", action="test")
    check("real build operation completed (result captured, regardless of pytest availability)", real_entry["event"] == "build_completed")
    check("captured exit_code", "exit_code" in real_entry)
    check("captured stdout/stderr", "stdout" in real_entry and "stderr" in real_entry)
    check("captured duration_seconds", real_entry.get("duration_seconds", -1) >= 0)
    check("captured build status", real_entry.get("status") in ("passed", "failed"))
    print(f"     python test outcome in this environment: status={real_entry.get('status')}, exit_code={real_entry.get('exit_code')}")

    print("\n== 8. Banned command denied before any approval/execution attempt ==")
    banned_entry = build_runner.run_build_operation("generic", venture_id="v-test", command=["rm", "-rf", "/"])
    check("banned build command denied, not executed", banned_entry["event"] == "build_denied")

    print("\n== 9. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    set_build_provider(_FakeProvider())
    try:
        build_runner.run_build_operation("python", venture_id="v-test", action="test")
    finally:
        set_build_provider(SubprocessBuildProvider())
    check("published build_started", "build_started" in seen_events)
    check("published build_completed", "build_completed" in seen_events)

    seen_events.clear()
    set_build_provider(_AlwaysFailingProvider())
    try:
        build_runner.run_build_operation("python", venture_id="v-test", action="test")
    finally:
        set_build_provider(SubprocessBuildProvider())
    check("published build_failed", "build_failed" in seen_events)

    print("\n== 10. Approval Framework: install (high risk) pauses inside a real graph ==")
    set_build_provider(_FakeProvider())
    try:

        class _S(TypedDict, total=False):
            history: Annotated[list, operator.add]

        def install_node(state: dict) -> dict:
            entry = build_runner.run_build_operation("python", venture_id="v-test", action="install")
            return {"history": [entry]}

        graph = StateGraph(_S)
        graph.add_node("install", install_node)
        graph.add_edge(START, "install")
        graph.add_edge("install", END)
        compiled = graph.compile(checkpointer=InMemorySaver())

        approve_thread = f"build-install-approve-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": approve_thread}}
        first = compiled.invoke({"history": []}, config=config)
        check("install paused for human approval (real interrupt)", "__interrupt__" in first)
        check("interrupt carries the build_python action", first["__interrupt__"][0].value["action"] == "build_python")

        final = compiled.invoke(Command(resume={"approved": True, "reason": "reviewed"}), config=config)
        check("resumed past the gate after approval", "__interrupt__" not in final)
        check("install executed after approval", final["history"][0]["event"] == "build_completed")

        reject_thread = f"build-install-reject-{uuid.uuid4().hex[:8]}"
        config2 = {"configurable": {"thread_id": reject_thread}}
        compiled.invoke({"history": []}, config=config2)
        final2 = compiled.invoke(Command(resume={"approved": False, "reason": "not reviewed"}), config=config2)
        check("rejected install recorded as denied, never executed", final2["history"][0]["event"] == "build_denied")
    finally:
        set_build_provider(SubprocessBuildProvider())

    print("\n== 11. Manager-callable node shape ==")
    set_build_provider(_FakeProvider())
    try:
        delta = build_runner.build_runner_node({"venture_id": "v-test"})
        check("build_runner_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
        check("build_runner_node's test action completed", delta["history"][0]["event"] == "build_completed")
    finally:
        set_build_provider(SubprocessBuildProvider())

    print("\nAll Phase 2 Component 5 checks passed.")


if __name__ == "__main__":
    main()
