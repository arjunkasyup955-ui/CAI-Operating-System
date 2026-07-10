"""Phase 2, Component 2: Git Integration.

Verifies:
  - Agent Registration + manifest (7 tools, git_ops permission)
  - Permission enforcement (agent without git_ops is denied)
  - Retry behaviour (ToolRegistry's existing RetryPolicy, exercised via a flaky
    injected provider)
  - Graceful failure handling (provider raises -> git_operation_failed, never crashes)
  - Event Publishing (git_operation_started/completed/failed/denied)
  - Provider injection (fake provider for mutating ops - the real repo is never
    touched by add/commit/checkout in this test)
  - Destructive-operation refusal (reset --hard / clean -fd / push --force)
  - Approval Framework: checkout (high risk) genuinely pauses for human approval
    inside a real LangGraph graph, and both the approve and reject paths behave
    correctly
  - Read-only operations (status/diff/log/branch) against the real repository - safe,
    no mutation

Run: python scripts/smoke_test_phase2_git_agent.py
"""

import logging
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import agents.coding.git.agent as git_agent  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import get_agent_registry, get_tool_registry  # noqa: E402
from tools.git.git_providers import (  # noqa: E402
    DestructiveGitOperationError,
    GitOperationResult,
    SubprocessGitProvider,
    set_git_provider,
)


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


class _FakeGitProvider:
    name = "fake_git"

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[list[str]] = []
        self._fail_times = fail_times
        self._call_count = 0

    def run(self, args: list[str]) -> GitOperationResult:
        self.calls.append(args)
        self._call_count += 1
        if self._call_count <= self._fail_times:
            raise RuntimeError(f"simulated transient failure (call {self._call_count})")
        return GitOperationResult(success=True, operation=" ".join(args), output="fake output", exit_code=0)


class _AlwaysFailingProvider:
    name = "always_failing"

    def run(self, args: list[str]) -> GitOperationResult:
        raise RuntimeError("simulated permanent failure")


def main() -> None:
    print("\n== 1. Agent Registration + Manifest ==")
    manifest = get_agent_registry().get("git_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares git_ops permission", manifest.permissions == ["git_ops"])
    check(
        "declares all 7 tools",
        set(manifest.tools) == {"git_status", "git_add", "git_commit", "git_checkout", "git_branch", "git_diff", "git_log"},
    )
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Tool Registry: schema + permission enforcement (no special-casing) ==")
    spec = get_tool_registry().get_spec("git_commit")
    check("git_commit has an input schema", spec.input_schema is not None)
    check("git_commit requires git_ops permission", "git_ops" in spec.permissions)
    try:
        get_tool_registry().invoke("git_commit", agent_name="git_agent")
        schema_rejected = False
    except ValidationError:
        schema_rejected = True
    check("missing required 'message' field rejected before execution", schema_rejected)

    manifest_no_perms = manifest.model_copy(update={"name": "no_git_perms_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("git_status", agent_name="no_git_perms_agent")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without git_ops is denied", denied)

    print("\n== 3. Destructive-operation refusal (defense in depth) ==")
    provider = SubprocessGitProvider()
    for banned_args in (["reset", "--hard"], ["clean", "-fd"], ["clean", "-f"], ["push", "--force"], ["push", "-f"]):
        try:
            provider.run(banned_args)
            refused = False
        except DestructiveGitOperationError:
            refused = True
        check(f"refuses 'git {' '.join(banned_args)}'", refused)

    try:
        from tools.git.git import git_add

        git_add(paths=["--force"])
        flag_rejected = False
    except ValueError:
        flag_rejected = True
    check("rejects a flag-like path (flag-smuggling prevention)", flag_rejected)

    print("\n== 4. Provider Injection + Retry Behaviour (mutating ops, fake provider only) ==")
    flaky = _FakeGitProvider(fail_times=1)
    set_git_provider(flaky)
    try:
        add_entry = git_agent.run_git_operation("add", venture_id="v-test", paths=["README.md"])
        check("git_add retried past one transient failure and completed", add_entry["event"] == "git_operation_completed")
        check("ToolRegistry's RetryPolicy caused a second attempt", len(flaky.calls) == 2)

        commit_entry = git_agent.run_git_operation("commit", venture_id="v-test", message="test commit")
        check("git_commit completed via the fake provider", commit_entry["event"] == "git_operation_completed")
        check("real repo never touched (fake provider recorded the calls instead)", ["commit", "-m", "test commit"] in flaky.calls)
    finally:
        set_git_provider(SubprocessGitProvider())

    print("\n== 5. Graceful Failure Handling ==")
    set_git_provider(_AlwaysFailingProvider())
    try:
        fail_entry = git_agent.run_git_operation("add", venture_id="v-test", paths=["x.txt"])
        check("provider exception caught and returned as git_operation_failed, never raised", fail_entry["event"] == "git_operation_failed")
    finally:
        set_git_provider(SubprocessGitProvider())

    print("\n== 6. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    set_git_provider(_FakeGitProvider())
    try:
        git_agent.run_git_operation("status", venture_id="v-test")
    finally:
        set_git_provider(SubprocessGitProvider())
    check("published git_operation_started", "git_operation_started" in seen_events)
    check("published git_operation_completed", "git_operation_completed" in seen_events)

    seen_events.clear()
    set_git_provider(_AlwaysFailingProvider())
    try:
        git_agent.run_git_operation("status", venture_id="v-test")
    finally:
        set_git_provider(SubprocessGitProvider())
    check("published git_operation_failed", "git_operation_failed" in seen_events)

    print("\n== 7. Approval Framework: checkout (high risk) pauses inside a real graph ==")
    set_git_provider(_FakeGitProvider())
    try:

        def checkout_node(state: dict) -> dict:
            entry = git_agent.run_git_operation("checkout", venture_id="v-test", ref="feature-branch")
            return {"history": [entry]}

        graph = StateGraph(dict)
        graph.add_node("checkout", checkout_node)
        graph.add_edge(START, "checkout")
        graph.add_edge("checkout", END)
        compiled = graph.compile(checkpointer=InMemorySaver())

        approve_thread = f"git-checkout-approve-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": approve_thread}}
        first = compiled.invoke({"history": []}, config=config)
        check("checkout paused for human approval (real interrupt)", "__interrupt__" in first)
        check("interrupt carries the git_checkout action", first["__interrupt__"][0].value["action"] == "git_checkout")

        final = compiled.invoke(Command(resume={"approved": True, "reason": "reviewed"}), config=config)
        check("resumed past the gate after approval", "__interrupt__" not in final)
        check("checkout executed after approval", final["history"][0]["event"] == "git_operation_completed")

        reject_thread = f"git-checkout-reject-{uuid.uuid4().hex[:8]}"
        config2 = {"configurable": {"thread_id": reject_thread}}
        compiled.invoke({"history": []}, config=config2)
        final2 = compiled.invoke(Command(resume={"approved": False, "reason": "not reviewed"}), config=config2)
        check("rejected checkout recorded as denied, never executed", final2["history"][0]["event"] == "git_operation_denied")
    finally:
        set_git_provider(SubprocessGitProvider())

    print("\n== 8. Read-only operations against the real repository (safe, no mutation) ==")
    status_entry = git_agent.run_git_operation("status", venture_id="v-test")
    check("git_status completed against the real repo", status_entry["event"] == "git_operation_completed")
    log_entry = git_agent.run_git_operation("log", venture_id="v-test", max_count=3)
    check("git_log completed against the real repo", log_entry["event"] == "git_operation_completed")
    diff_entry = git_agent.run_git_operation("diff", venture_id="v-test")
    check("git_diff completed against the real repo", diff_entry["event"] == "git_operation_completed")
    branch_entry = git_agent.run_git_operation("branch", venture_id="v-test")
    check("git_branch (list) completed against the real repo", branch_entry["event"] == "git_operation_completed")

    print("\n== 9. Manager-callable node shape ==")
    delta: dict[str, Any] = git_agent.git_agent_node({"venture_id": "v-test"})
    check("git_agent_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
    check("git_agent_node's status check completed", delta["history"][0]["event"] == "git_operation_completed")

    print("\nAll Phase 2 Component 2 checks passed.")


if __name__ == "__main__":
    main()
