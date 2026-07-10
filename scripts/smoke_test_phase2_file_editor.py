"""Phase 2, Component 3: File Editing Engine.

Verifies:
  - Agent Registration + manifest (6 tools, file_access permission)
  - Schema validation
  - Permission enforcement
  - Retry behaviour (ToolRegistry's existing RetryPolicy, via a flaky fake provider)
  - Graceful failure handling (provider raises -> file_edit_failed, never crashes)
  - Dependency injection (fake + temp-rooted real provider - the actual repo is never
    touched by this test)
  - Approval Framework: write_file (high risk) genuinely pauses for human approval
    inside a real LangGraph graph, both approve and reject paths
  - Path traversal protection
  - Protected directory/file protection (.git, .venv, core/ kernel, .env)
  - Event Publishing (file_edit_started/completed/failed/denied)

Run: python scripts/smoke_test_phase2_file_editor.py
"""

import logging
import sys
import tempfile
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

import agents.coding.file_editor.agent as file_editor  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import get_agent_registry, get_tool_registry  # noqa: E402
from tools.files.file_providers import (  # noqa: E402
    FileOperationResult,
    LocalFileSystemProvider,
    ProtectedPathError,
    resolve_safe_path,
    set_file_provider,
)


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


class _FlakyProvider:
    name = "flaky"

    def __init__(self, fail_times: int = 1) -> None:
        self.calls = 0
        self._fail_times = fail_times

    def read_file(self, path: str) -> FileOperationResult:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError(f"simulated transient failure (call {self.calls})")
        return FileOperationResult(success=True, operation="read_file", path=path, content="ok")

    def write_file(self, path, content):
        raise NotImplementedError

    def append_file(self, path, content):
        raise NotImplementedError

    def replace_text(self, path, old_text, new_text):
        raise NotImplementedError

    def create_file(self, path, content=""):
        raise NotImplementedError

    def create_directory(self, path):
        raise NotImplementedError


class _AlwaysFailingProvider(_FlakyProvider):
    def __init__(self) -> None:
        super().__init__(fail_times=10**9)


def main() -> None:
    print("\n== 1. Agent Registration + Manifest ==")
    manifest = get_agent_registry().get("file_editor_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares file_access permission", manifest.permissions == ["file_access"])
    check(
        "declares all 6 tools",
        set(manifest.tools) == {"read_file", "write_file", "append_file", "replace_text", "create_file", "create_directory"},
    )
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Schema Validation ==")
    try:
        get_tool_registry().invoke("write_file", agent_name="file_editor_agent", path="x.txt")
        schema_rejected = False
    except ValidationError:
        schema_rejected = True
    check("missing required 'content' field rejected before execution", schema_rejected)

    print("\n== 3. Permission Enforcement ==")
    manifest_no_perms = manifest.model_copy(update={"name": "no_file_perms_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("read_file", agent_name="no_file_perms_agent", path="x.txt")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without file_access is denied", denied)

    print("\n== 4. Path Traversal + Protected Path Protection ==")
    root = Path(__file__).resolve().parent.parent
    for bad_path, label in (
        ("../outside.txt", "parent traversal"),
        ("agents/../../../evil.txt", "nested traversal"),
        ("C:/Windows/System32/evil.txt", "absolute path"),
    ):
        try:
            resolve_safe_path(bad_path, root)
            rejected = False
        except Exception:
            rejected = True
        check(f"rejects {label}: {bad_path!r}", rejected)

    for protected_path in (".git/config", ".venv/pyvenv.cfg", "core/state.py", ".env"):
        try:
            resolve_safe_path(protected_path, root)
            rejected = False
        except ProtectedPathError:
            rejected = True
        check(f"rejects protected path: {protected_path!r}", rejected)

    print("\n== 5. Protected-path violations surface as graceful failure via the agent (never crash) ==")
    kernel_entry = file_editor.run_file_operation("read_file", venture_id="v-test", path="core/state.py")
    check("touching core/ kernel gracefully fails", kernel_entry["event"] == "file_edit_failed")
    traversal_entry = file_editor.run_file_operation("read_file", venture_id="v-test", path="../outside.txt")
    check("path traversal attempt gracefully fails", traversal_entry["event"] == "file_edit_failed")

    print("\n== 6. Retry Behaviour (ToolRegistry's existing RetryPolicy) ==")
    flaky = _FlakyProvider(fail_times=1)
    set_file_provider(flaky)
    try:
        retry_entry = file_editor.run_file_operation("read_file", venture_id="v-test", path="whatever.txt")
        check("retried past one transient failure and completed", retry_entry["event"] == "file_edit_completed")
        check("exactly 2 attempts were made", flaky.calls == 2)
    finally:
        set_file_provider(LocalFileSystemProvider())

    print("\n== 7. Graceful Failure Handling ==")
    set_file_provider(_AlwaysFailingProvider())
    try:
        fail_entry = file_editor.run_file_operation("read_file", venture_id="v-test", path="whatever.txt")
        check("exhausted retries return file_edit_failed, never raises", fail_entry["event"] == "file_edit_failed")
    finally:
        set_file_provider(LocalFileSystemProvider())

    print("\n== 8. Dependency Injection: full real I/O lifecycle against an isolated temp root ==")
    with tempfile.TemporaryDirectory() as tmp:
        set_file_provider(LocalFileSystemProvider(root=tmp))
        try:
            create_entry = file_editor.run_file_operation("create_file", venture_id="v-test", path="notes.txt", content="hello world")
            check("create_file completed", create_entry["event"] == "file_edit_completed" and create_entry["success"])

            create_again = file_editor.run_file_operation("create_file", venture_id="v-test", path="notes.txt", content="dup")
            check("create_file refuses to overwrite an existing file", create_again["success"] is False)

            read_entry = file_editor.run_file_operation("read_file", venture_id="v-test", path="notes.txt")
            check("read_file returns the written content", read_entry["content"] == "hello world")

            file_editor.run_file_operation("append_file", venture_id="v-test", path="notes.txt", content=" appended")
            file_editor.run_file_operation("replace_text", venture_id="v-test", path="notes.txt", old_text="hello", new_text="goodbye")
            final_read = file_editor.run_file_operation("read_file", venture_id="v-test", path="notes.txt")
            check("append + replace_text produced the expected final content", final_read["content"] == "goodbye world appended")

            mkdir_entry = file_editor.run_file_operation("create_directory", venture_id="v-test", path="subdir/nested")
            check("create_directory completed", mkdir_entry["event"] == "file_edit_completed")
            check("directory actually exists on disk (isolated temp root)", (Path(tmp) / "subdir" / "nested").is_dir())
        finally:
            set_file_provider(LocalFileSystemProvider())

    print("\n== 9. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    with tempfile.TemporaryDirectory() as tmp:
        set_file_provider(LocalFileSystemProvider(root=tmp))
        try:
            file_editor.run_file_operation("create_directory", venture_id="v-test", path="newdir")
        finally:
            set_file_provider(LocalFileSystemProvider())
    check("published file_edit_started", "file_edit_started" in seen_events)
    check("published file_edit_completed", "file_edit_completed" in seen_events)

    seen_events.clear()
    set_file_provider(_AlwaysFailingProvider())
    try:
        file_editor.run_file_operation("read_file", venture_id="v-test", path="whatever.txt")
    finally:
        set_file_provider(LocalFileSystemProvider())
    check("published file_edit_failed", "file_edit_failed" in seen_events)

    print("\n== 10. Approval Framework: write_file (high risk) pauses inside a real graph ==")
    with tempfile.TemporaryDirectory() as tmp:
        set_file_provider(LocalFileSystemProvider(root=tmp))
        try:

            class _S(TypedDict, total=False):
                history: Annotated[list, operator.add]

            def make_write_node(path: str):
                def write_node(state: dict) -> dict:
                    entry = file_editor.run_file_operation(
                        "write_file", venture_id="v-test", path=path, content="new content"
                    )
                    return {"history": [entry]}

                return write_node

            approve_graph = StateGraph(_S)
            approve_graph.add_node("write", make_write_node("approved.txt"))
            approve_graph.add_edge(START, "write")
            approve_graph.add_edge("write", END)
            compiled_approve = approve_graph.compile(checkpointer=InMemorySaver())

            approve_thread = f"file-write-approve-{uuid.uuid4().hex[:8]}"
            config = {"configurable": {"thread_id": approve_thread}}
            first = compiled_approve.invoke({"history": []}, config=config)
            check("write_file paused for human approval (real interrupt)", "__interrupt__" in first)
            check("interrupt carries the write_file action", first["__interrupt__"][0].value["action"] == "write_file")

            final = compiled_approve.invoke(Command(resume={"approved": True, "reason": "reviewed"}), config=config)
            check("resumed past the gate after approval", "__interrupt__" not in final)
            check("write_file executed after approval", final["history"][0]["event"] == "file_edit_completed")
            check("approved write actually landed on disk", (Path(tmp) / "approved.txt").read_text() == "new content")

            reject_graph = StateGraph(_S)
            reject_graph.add_node("write", make_write_node("rejected.txt"))
            reject_graph.add_edge(START, "write")
            reject_graph.add_edge("write", END)
            compiled_reject = reject_graph.compile(checkpointer=InMemorySaver())

            reject_thread = f"file-write-reject-{uuid.uuid4().hex[:8]}"
            config2 = {"configurable": {"thread_id": reject_thread}}
            compiled_reject.invoke({"history": []}, config=config2)
            final2 = compiled_reject.invoke(Command(resume={"approved": False, "reason": "not reviewed"}), config=config2)
            check("rejected write_file recorded as denied, never executed", final2["history"][0]["event"] == "file_edit_denied")
            check("rejected write never touched disk", not (Path(tmp) / "rejected.txt").exists())
        finally:
            set_file_provider(LocalFileSystemProvider())

    print("\n== 11. Manager-callable node shape ==")
    with tempfile.TemporaryDirectory() as tmp:
        set_file_provider(LocalFileSystemProvider(root=tmp))
        try:
            delta = file_editor.file_editor_node({"venture_id": "v-test"})
            check("file_editor_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
            check("file_editor_node's create_directory completed", delta["history"][0]["event"] == "file_edit_completed")
        finally:
            set_file_provider(LocalFileSystemProvider())

    print("\nAll Phase 2 Component 3 checks passed.")


if __name__ == "__main__":
    main()
