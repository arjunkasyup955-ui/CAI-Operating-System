"""Phase 2, Component 1: Claude Code Integration.

Verifies:
  - Agent Registration
  - Manifest
  - Structured request/response models (ClaudeCodeTaskRequest / ClaudeCodeTaskResult)
  - Dependency Injection (provider=)
  - Retry behaviour using the kernel's existing RetryPolicy (core.registries.tool_registry)
  - Graceful Skip Behaviour (empty task_description)
  - Graceful Failure Handling (retries exhausted, never raises)
  - Event Publishing (claude_code_started/completed/failed)
  - Manager-callable node shape (claude_code_node)

Run: python scripts/smoke_test_phase2_claude_code.py
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

import agents.coding.claude_code.agent as claude_code  # noqa: E402
from agents.coding.claude_code.providers import ClaudeCodeTaskRequest, ClaudeCodeTaskResult  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.registries import get_agent_registry  # noqa: E402
from core.registries.tool_registry import RetryPolicy  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


class _FakeProvider:
    name = "fake_provider"

    def run_task(self, request: ClaudeCodeTaskRequest) -> ClaudeCodeTaskResult:
        return ClaudeCodeTaskResult(
            success=True,
            summary="Add a /health endpoint returning 200 OK",
            plan=["Locate the router module", "Add a GET /health handler", "Add a test for it"],
            output="raw model output",
            provider=self.name,
        )


class _FlakyProvider:
    """Fails once, then succeeds - proves the kernel RetryPolicy is actually retried,
    not just configured."""

    def __init__(self) -> None:
        self.name = "flaky_provider"
        self.calls = 0

    def run_task(self, request: ClaudeCodeTaskRequest) -> ClaudeCodeTaskResult:
        self.calls += 1
        if self.calls < 2:
            raise RuntimeError("simulated transient failure")
        return ClaudeCodeTaskResult(success=True, summary="ok after retry", provider=self.name)


class _AlwaysFailingProvider:
    name = "always_failing"

    def run_task(self, request: ClaudeCodeTaskRequest) -> ClaudeCodeTaskResult:
        raise RuntimeError("simulated permanent failure")


def main() -> None:
    print("\n== PASS Agent Registration & Manifest ==")
    manifest = get_agent_registry().get("claude_code_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares no tools (delegation-only in this component)", manifest.tools == [])
    check("declares no permissions (no ToolRegistry calls yet)", manifest.permissions == [])
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== PASS Structured Request/Response Models ==")
    request = ClaudeCodeTaskRequest(task_description="Add a /health endpoint", venture_id="venture-cc-test")
    check("request has task_description", request.task_description == "Add a /health endpoint")
    result = ClaudeCodeTaskResult(success=True, summary="s", plan=["a"], provider="x")
    check("result has files_changed empty by design (no file editing yet)", result.files_changed == [])
    check("result has commands_run empty by design (no terminal execution yet)", result.commands_run == [])

    print("\n== PASS Dependency Injection ==")
    entry = claude_code.run_claude_code_delegation(request, provider=_FakeProvider())
    check("produced claude_code_completed via injected fake provider", entry["event"] == "claude_code_completed")
    check("summary matches the injected deterministic value", entry["summary"] == "Add a /health endpoint returning 200 OK")
    check("plan matches the injected deterministic value", entry["plan"] == ["Locate the router module", "Add a GET /health handler", "Add a test for it"])
    check("files_changed empty (no file editing yet)", entry["files_changed"] == [])
    check("commands_run empty (no terminal execution yet)", entry["commands_run"] == [])

    print("\n== PASS Retry Behaviour (kernel RetryPolicy) ==")
    flaky = _FlakyProvider()
    retry_entry = claude_code.run_claude_code_delegation(
        request, provider=flaky, retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=0.01)
    )
    check("retried past one transient failure", flaky.calls == 2)
    check("succeeded after retry", retry_entry["event"] == "claude_code_completed")

    print("\n== PASS Skip Behaviour ==")
    skip_entry = claude_code.run_claude_code_delegation(ClaudeCodeTaskRequest(task_description="   "), provider=_FakeProvider())
    check("skips instead of raising on an empty task_description", skip_entry["event"] == "claude_code_skipped")

    print("\n== PASS Failure Handling ==")
    fail_entry = claude_code.run_claude_code_delegation(
        request, provider=_AlwaysFailingProvider(), retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=0.01)
    )
    check("exhausts retries and returns claude_code_failed, never raises", fail_entry["event"] == "claude_code_failed")

    print("\n== PASS Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    claude_code.run_claude_code_delegation(request, provider=_FakeProvider())
    check("published claude_code_started", "claude_code_started" in seen_events)
    check("published claude_code_completed", "claude_code_completed" in seen_events)

    seen_events.clear()
    claude_code.run_claude_code_delegation(
        request, provider=_AlwaysFailingProvider(), retry_policy=RetryPolicy(max_attempts=1, backoff_seconds=0.01)
    )
    check("published claude_code_failed", "claude_code_failed" in seen_events)

    seen_events.clear()
    claude_code.run_claude_code_delegation(ClaudeCodeTaskRequest(task_description=""), provider=_FakeProvider())
    check("published claude_code_skipped", "claude_code_skipped" in seen_events)

    print("\n== PASS Manager-callable node shape ==")
    delta = claude_code.claude_code_node({"idea": "", "venture_id": "venture-cc-test"})
    check("claude_code_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
    check("claude_code_node gracefully skips on an empty idea", delta["history"][0]["event"] == "claude_code_skipped")

    print("\nAll Phase 2 Component 1 checks passed.")


if __name__ == "__main__":
    main()
