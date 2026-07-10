"""Phase 3, Component 10: MCP Server Integration.

Verifies:
  - Registration (agent + 8 tools)
  - Permissions (internet_access, enforced)
  - Schema validation
  - Retry (ToolRegistry's existing RetryPolicy, via a flaky fake provider)
  - Timeout (ToolRegistry's timeout mechanism, via a deliberately slow function)
  - Graceful failure (real provider - mcp SDK not installed - never crashes, for
    both health_check with/without a server_name and a mutating operation)
  - Fake provider server lifecycle (list_servers -> unavailable-server-name fails ->
    unreachable-server simulates connection refused -> connect_server -> list_tools
    -> disconnect_server -> use-after-disconnect fails, deterministic, in-memory)
  - Tool execution (including invalid tool name and malformed response failure
    scenarios)
  - Resource reading (including malformed response failure scenario)
  - Prompt execution
  - Event publishing (mcp_started/completed/failed)
  - Manager node integration
  - Regression of every previous component (run separately, see the full suite this
    script is part of)

Run: python scripts/smoke_test_phase3_mcp.py
"""

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from pydantic import ValidationError, create_model  # noqa: E402

import agents.infrastructure.mcp.agent as mcp_agent  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import ToolSpec, get_agent_registry, get_tool_registry  # noqa: E402
from core.registries.tool_registry import RetryPolicy  # noqa: E402
from tools.mcp.mcp_providers import (  # noqa: E402
    FakeMCPProvider,
    MCPHealthStatus,
    MCPSDKProvider,
    set_mcp_provider,
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

    def health_check(self, server_name: str | None = None) -> MCPHealthStatus:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("transient failure")
        return MCPHealthStatus(healthy=True, installed=True)


def main() -> None:
    expected_tools = {
        "mcp_health_check", "mcp_list_servers", "mcp_connect_server", "mcp_disconnect_server",
        "mcp_list_tools", "mcp_call_tool", "mcp_read_resource", "mcp_execute_prompt",
    }

    print("\n== 1. Registration (agent + 8 tools) ==")
    manifest = get_agent_registry().get("mcp_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares internet_access permission", set(manifest.permissions) == {"internet_access"})
    check("declares all 8 tools", set(manifest.tools) == expected_tools)
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Permissions ==")
    for tool_name in expected_tools:
        spec = get_tool_registry().get_spec(tool_name)
        check(f"{tool_name} requires internet_access", "internet_access" in spec.permissions)

    manifest_no_perms = manifest.model_copy(update={"name": "no_net_mcp_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("mcp_health_check", agent_name="no_net_mcp_agent")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without internet_access is denied", denied)

    try:
        get_tool_registry().invoke("mcp_health_check", agent_name="git_agent")
        denied_other = False
    except PermissionDeniedError:
        denied_other = True
    check("an unrelated registered agent (git_agent) is denied", denied_other)

    print("\n== 3. Schema Validation ==")
    try:
        get_tool_registry().invoke("mcp_connect_server", agent_name="mcp_agent")
        rejected = False
    except ValidationError:
        rejected = True
    check("missing required server_name rejected", rejected)

    try:
        get_tool_registry().invoke("mcp_call_tool", agent_name="mcp_agent", session_id="session-1")
        rejected2 = False
    except ValidationError:
        rejected2 = True
    check("missing required tool_name rejected", rejected2)

    print("\n== 4. Retry Behaviour (ToolRegistry's existing RetryPolicy) ==")
    flaky = _FlakyProvider(fail_times=1)
    set_mcp_provider(flaky)
    try:
        retry_entry = mcp_agent.run_mcp_operation("mcp_health_check", venture_id="v-test")
        check("retried past one transient failure and completed", retry_entry["event"] == "mcp_completed")
        check("exactly 2 attempts were made", flaky.calls == 2)
    finally:
        set_mcp_provider(MCPSDKProvider())

    print("\n== 5. Timeout (ToolRegistry's timeout mechanism) ==")
    def _slow_query():
        time.sleep(2.0)
        return {"success": True}

    get_tool_registry().register(
        ToolSpec(
            name="mcp_test_slow_query",
            input_schema=create_model("SlowQueryArgs"),
            permissions=["internet_access"],
            retry_policy=RetryPolicy(max_attempts=1),
            timeout_seconds=0.3,
        ),
        _slow_query,
    )
    start = time.monotonic()
    try:
        get_tool_registry().invoke("mcp_test_slow_query", agent_name="mcp_agent")
        timed_out = False
    except TimeoutError:
        timed_out = True
    elapsed = time.monotonic() - start
    check("a slow query times out per the tool's configured timeout_seconds", timed_out)
    check("did not wait for the full 2s operation to finish", elapsed < 1.5)

    print("\n== 6. Graceful Failure (real provider - mcp SDK not installed) ==")
    real_health = mcp_agent.run_mcp_operation("mcp_health_check", venture_id="v-test")
    check("real provider health check (no server) completes without crashing", real_health["event"] == "mcp_completed")
    check("reports unhealthy (mcp SDK not installed in this sandbox)", real_health["healthy"] is False)
    check("reports not installed", real_health["installed"] is False)
    print(f"     real provider error (expected): {real_health.get('error')}")

    real_health_named = mcp_agent.run_mcp_operation("mcp_health_check", venture_id="v-test", server_name="docs-server")
    check("real provider health check (named server) also completes without crashing", real_health_named["event"] == "mcp_completed")
    check("named health check also reports unhealthy", real_health_named["healthy"] is False)

    real_connect = mcp_agent.run_mcp_operation("mcp_connect_server", venture_id="v-test", server_name="docs-server")
    check("a mutating real-provider operation also fails gracefully without the SDK", real_connect["event"] == "mcp_failed")
    print(f"     real provider connect_server error (expected): {real_connect.get('error')}")

    print("\n== 7. Fake Provider: server lifecycle ==")
    set_mcp_provider(FakeMCPProvider())
    try:
        servers = mcp_agent.run_mcp_operation("mcp_list_servers", venture_id="v-test")
        check("list_servers returns the known fake servers", {s["server_name"] for s in servers["servers"]} == {"docs-server", "unreachable-server"})

        unavailable = mcp_agent.run_mcp_operation("mcp_connect_server", venture_id="v-test", server_name="ghost-server")
        check("connecting to an unconfigured server fails gracefully (server unavailable)", unavailable["event"] == "mcp_failed")

        refused = mcp_agent.run_mcp_operation("mcp_connect_server", venture_id="v-test", server_name="unreachable-server")
        check("connecting to 'unreachable-server' simulates connection refused", refused["event"] == "mcp_failed" and "refused" in refused["error"])

        conn = mcp_agent.run_mcp_operation("mcp_connect_server", venture_id="v-test", server_name="docs-server")
        check("connect_server returned a session_id", bool(conn.get("session_id")))
        sid = conn["session_id"]

        tools = mcp_agent.run_mcp_operation("mcp_list_tools", venture_id="v-test", session_id=sid)
        check("list_tools returns the fake server's tools", "search_docs" in {t["tool_name"] for t in tools["tools"]})

        print("\n== 8. Tool Execution ==")
        bad_tool = mcp_agent.run_mcp_operation("mcp_call_tool", venture_id="v-test", session_id=sid, tool_name="does-not-exist")
        check("calling an invalid tool name fails gracefully", bad_tool["event"] == "mcp_failed")

        malformed = mcp_agent.run_mcp_operation("mcp_call_tool", venture_id="v-test", session_id=sid, tool_name="malformed_tool")
        check("a tool returning a malformed response fails gracefully", malformed["event"] == "mcp_failed")

        result = mcp_agent.run_mcp_operation("mcp_call_tool", venture_id="v-test", session_id=sid, tool_name="search_docs", arguments={"query": "installation"})
        check("call_tool succeeds for a valid tool", result["success"])
        check("call_tool result reflects the arguments passed", "installation" in result["content"][0]["text"])

        print("\n== 9. Resource Reading ==")
        res = mcp_agent.run_mcp_operation("mcp_read_resource", venture_id="v-test", session_id=sid, resource_uri="docs://readme")
        check("read_resource succeeds for a known resource", res["success"])
        check("read_resource returns the fake content", "Fake README" in res["content"][0]["text"])

        bad_res = mcp_agent.run_mcp_operation("mcp_read_resource", venture_id="v-test", session_id=sid, resource_uri="docs://missing")
        check("reading an unknown resource fails gracefully", bad_res["event"] == "mcp_failed")

        malformed_res = mcp_agent.run_mcp_operation("mcp_read_resource", venture_id="v-test", session_id=sid, resource_uri="malformed://resource")
        check("reading a resource that returns a malformed response fails gracefully", malformed_res["event"] == "mcp_failed")

        print("\n== 10. Prompt Execution ==")
        prompt = mcp_agent.run_mcp_operation("mcp_execute_prompt", venture_id="v-test", session_id=sid, prompt_name="summarize", arguments={"text": "hello"})
        check("execute_prompt succeeds for a known prompt", prompt["success"])
        check("execute_prompt result reflects the arguments passed", "hello" in prompt["content"][0]["text"])

        bad_prompt = mcp_agent.run_mcp_operation("mcp_execute_prompt", venture_id="v-test", session_id=sid, prompt_name="does-not-exist")
        check("executing an unknown prompt fails gracefully", bad_prompt["event"] == "mcp_failed")

        disc = mcp_agent.run_mcp_operation("mcp_disconnect_server", venture_id="v-test", session_id=sid)
        check("disconnect_server succeeded", disc["success"])

        after_disc = mcp_agent.run_mcp_operation("mcp_list_tools", venture_id="v-test", session_id=sid)
        check("using a disconnected session fails gracefully", after_disc["event"] == "mcp_failed")
    finally:
        set_mcp_provider(MCPSDKProvider())

    print("\n== 11. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    mcp_agent.run_mcp_operation("mcp_health_check", venture_id="v-test")
    check("published mcp_started", "mcp_started" in seen_events)
    check("published mcp_completed", "mcp_completed" in seen_events)

    seen_events.clear()
    set_mcp_provider(_FlakyProvider(fail_times=10))
    try:
        mcp_agent.run_mcp_operation("mcp_health_check", venture_id="v-test")
    finally:
        set_mcp_provider(MCPSDKProvider())
    check("published mcp_failed", "mcp_failed" in seen_events)

    print("\n== 12. Manager-callable node shape ==")
    delta = mcp_agent.mcp_agent_node({"venture_id": "v-test"})
    check("mcp_agent_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
    check("node result reflects the health check operation", delta["history"][0]["operation"] == "mcp_health_check")

    print("\nAll Phase 3 Component 10 checks passed.")


if __name__ == "__main__":
    main()
