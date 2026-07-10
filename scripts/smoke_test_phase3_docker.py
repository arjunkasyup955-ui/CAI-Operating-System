"""Phase 3, Component 4: Docker Integration.

Verifies:
  - Registration (agent + 11 tools)
  - Permissions (deployment, + shell_exec for exec_command; enforced)
  - Schema validation
  - Retry (ToolRegistry's existing RetryPolicy, via a flaky fake provider)
  - Timeout (ToolRegistry's timeout mechanism, via a deliberately slow function)
  - Graceful failure (real provider - docker SDK not installed/functional - never
    crashes, even though Docker Desktop itself is actually running in this sandbox)
  - Fake provider lifecycle (pull -> create -> start -> logs -> restart -> stop ->
    remove, deterministic, in-memory)
  - Approval interrupts (remove_container and forced stop_container are high risk and
    genuinely pause inside a real graph)
  - Event publishing (docker_started/completed/failed)
  - Regression of every previous component (run separately, see the full suite this
    script is part of)

Run: python scripts/smoke_test_phase3_docker.py
"""

import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Annotated, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

import operator  # noqa: E402

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command  # noqa: E402
from pydantic import ValidationError, create_model  # noqa: E402

import agents.infrastructure.docker.agent as docker_agent  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import ToolSpec, get_agent_registry, get_tool_registry  # noqa: E402
from core.registries.tool_registry import RetryPolicy  # noqa: E402
from tools.docker.docker_providers import (  # noqa: E402
    DockerHealthStatus,
    DockerSDKProvider,
    FakeDockerProvider,
    set_docker_provider,
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

    def health_check(self) -> DockerHealthStatus:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("transient failure")
        return DockerHealthStatus(healthy=True, version="ok")


def main() -> None:
    print("\n== 1. Registration (agent + 11 tools) ==")
    manifest = get_agent_registry().get("docker_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares deployment + shell_exec permissions", set(manifest.permissions) == {"deployment", "shell_exec"})
    expected_tools = {
        "docker_health_check", "docker_list_containers", "docker_list_images", "docker_pull_image",
        "docker_create_container", "docker_start_container", "docker_stop_container", "docker_restart_container",
        "docker_remove_container", "docker_container_logs", "docker_exec_command",
    }
    check("declares all 11 tools", set(manifest.tools) == expected_tools)
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Permissions ==")
    for tool_name in expected_tools - {"docker_exec_command"}:
        spec = get_tool_registry().get_spec(tool_name)
        check(f"{tool_name} requires deployment", "deployment" in spec.permissions)
    exec_spec = get_tool_registry().get_spec("docker_exec_command")
    check("docker_exec_command requires both deployment and shell_exec", set(exec_spec.permissions) == {"deployment", "shell_exec"})

    manifest_no_perms = manifest.model_copy(update={"name": "no_deploy_docker_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("docker_health_check", agent_name="no_deploy_docker_agent")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without deployment is denied", denied)

    manifest_deploy_only = manifest.model_copy(update={"name": "deploy_only_docker_agent", "permissions": ["deployment"]})
    get_agent_registry().register(manifest_deploy_only)
    try:
        get_tool_registry().invoke("docker_exec_command", agent_name="deploy_only_docker_agent", container_id="x", command=["ls"])
        denied_exec = False
    except PermissionDeniedError:
        denied_exec = True
    check("exec_command denied without shell_exec even with deployment granted", denied_exec)

    print("\n== 3. Schema Validation ==")
    try:
        get_tool_registry().invoke("docker_exec_command", agent_name="docker_agent", container_id="x", command=[])
        rejected = False
    except ValidationError:
        rejected = True
    check("empty command list rejected (min_length=1)", rejected)

    print("\n== 4. Retry Behaviour (ToolRegistry's existing RetryPolicy) ==")
    flaky = _FlakyProvider(fail_times=1)
    set_docker_provider(flaky)
    try:
        retry_entry = docker_agent.run_docker_operation("docker_health_check", venture_id="v-test")
        check("retried past one transient failure and completed", retry_entry["event"] == "docker_completed")
        check("exactly 2 attempts were made", flaky.calls == 2)
    finally:
        set_docker_provider(DockerSDKProvider())

    print("\n== 5. Timeout (ToolRegistry's timeout mechanism) ==")
    def _slow_query():
        time.sleep(2.0)
        return {"success": True}

    get_tool_registry().register(
        ToolSpec(
            name="docker_test_slow_query",
            input_schema=create_model("SlowQueryArgs"),
            permissions=["deployment"],
            retry_policy=RetryPolicy(max_attempts=1),
            timeout_seconds=0.3,
        ),
        _slow_query,
    )
    start = time.monotonic()
    try:
        get_tool_registry().invoke("docker_test_slow_query", agent_name="docker_agent")
        timed_out = False
    except TimeoutError:
        timed_out = True
    elapsed = time.monotonic() - start
    check("a slow query times out per the tool's configured timeout_seconds", timed_out)
    check("did not wait for the full 2s operation to finish", elapsed < 1.5)

    print("\n== 6. Graceful Failure (real provider - docker SDK not installed/functional) ==")
    real_health = docker_agent.run_docker_operation("docker_health_check", venture_id="v-test")
    check("real provider health check completes without crashing", real_health["event"] == "docker_completed")
    check("reports unhealthy (docker SDK not installed in this sandbox)", real_health["healthy"] is False)
    print(f"     real provider error (expected): {real_health.get('error')}")

    print("\n== 7. Fake Provider: full container lifecycle ==")
    set_docker_provider(FakeDockerProvider())
    try:
        pull = docker_agent.run_docker_operation("docker_pull_image", venture_id="v-test", image_ref="hello-world:latest")
        check("pull_image succeeded", pull["success"])

        imgs = docker_agent.run_docker_operation("docker_list_images", venture_id="v-test")
        check("pulled image appears in list_images", any("hello-world:latest" in i["repo_tags"] for i in imgs["images"]))

        create = docker_agent.run_docker_operation("docker_create_container", venture_id="v-test", image_ref="hello-world:latest", container_name="afos-smoketest")
        check("create_container returned a container_id", bool(create["container_id"]))
        cid = create["container_id"]

        listed = docker_agent.run_docker_operation("docker_list_containers", venture_id="v-test")
        check("created container appears with state 'created'", listed["containers"][0]["state"] == "created")

        start_result = docker_agent.run_docker_operation("docker_start_container", venture_id="v-test", container_id=cid)
        check("start_container succeeded", start_result["success"])

        logs = docker_agent.run_docker_operation("docker_container_logs", venture_id="v-test", container_id=cid)
        check("container_logs returns output", "Hello from" in logs["output"])

        restart_result = docker_agent.run_docker_operation("docker_restart_container", venture_id="v-test", container_id=cid)
        check("restart_container succeeded", restart_result["success"])

        stop_result = docker_agent.run_docker_operation("docker_stop_container", venture_id="v-test", container_id=cid)
        check("stop_container succeeded", stop_result["success"])

        listed2 = docker_agent.run_docker_operation("docker_list_containers", venture_id="v-test")
        check("container state is 'exited' after stop", listed2["containers"][0]["state"] == "exited")

        print("\n== 8. Approval Interrupts: remove_container (high risk) ==")

        class _S(TypedDict, total=False):
            history: Annotated[list, operator.add]

        def remove_node(state: dict) -> dict:
            entry = docker_agent.run_docker_operation("docker_remove_container", venture_id="v-test", container_id=cid, force=True)
            return {"history": [entry]}

        graph = StateGraph(_S)
        graph.add_node("remove", remove_node)
        graph.add_edge(START, "remove")
        graph.add_edge("remove", END)
        compiled = graph.compile(checkpointer=InMemorySaver())

        approve_thread = f"docker-remove-approve-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": approve_thread}}
        first = compiled.invoke({"history": []}, config=config)
        check("remove_container paused for human approval (real interrupt)", "__interrupt__" in first)
        check("interrupt carries the docker_remove_container action", first["__interrupt__"][0].value["action"] == "docker_remove_container")

        final = compiled.invoke(Command(resume={"approved": True, "reason": "reviewed"}), config=config)
        check("resumed past the gate after approval", "__interrupt__" not in final)
        check("remove executed after approval", final["history"][0]["event"] == "docker_completed")

        listed3 = docker_agent.run_docker_operation("docker_list_containers", venture_id="v-test")
        check("container genuinely removed after approval", len(listed3["containers"]) == 0)

        print("\n== 8b. Approval Interrupts: forced stop escalates to high risk ==")
        create2 = docker_agent.run_docker_operation("docker_create_container", venture_id="v-test", image_ref="hello-world:latest")
        cid2 = create2["container_id"]
        docker_agent.run_docker_operation("docker_start_container", venture_id="v-test", container_id=cid2)

        def force_stop_node(state: dict) -> dict:
            entry = docker_agent.run_docker_operation("docker_stop_container", venture_id="v-test", container_id=cid2, force=True)
            return {"history": [entry]}

        graph2 = StateGraph(_S)
        graph2.add_node("stop", force_stop_node)
        graph2.add_edge(START, "stop")
        graph2.add_edge("stop", END)
        compiled2 = graph2.compile(checkpointer=InMemorySaver())
        config2 = {"configurable": {"thread_id": f"docker-force-stop-{uuid.uuid4().hex[:8]}"}}
        first2 = compiled2.invoke({"history": []}, config=config2)
        check("forced stop_container paused for approval (escalated risk)", "__interrupt__" in first2)

        print("\n== 8c. Approval Interrupts: exec_command (high risk) ==")

        def exec_node(state: dict) -> dict:
            entry = docker_agent.run_docker_operation("docker_exec_command", venture_id="v-test", container_id=cid2, command=["echo", "hi"])
            return {"history": [entry]}

        graph3 = StateGraph(_S)
        graph3.add_node("exec", exec_node)
        graph3.add_edge(START, "exec")
        graph3.add_edge("exec", END)
        compiled3 = graph3.compile(checkpointer=InMemorySaver())
        config3 = {"configurable": {"thread_id": f"docker-exec-approve-{uuid.uuid4().hex[:8]}"}}
        first3 = compiled3.invoke({"history": []}, config=config3)
        check("exec_command paused for human approval (real interrupt)", "__interrupt__" in first3)

        final3 = compiled3.invoke(Command(resume={"approved": True, "reason": "reviewed"}), config=config3)
        check("resumed past the gate after approval", "__interrupt__" not in final3)
        check("exec_command executed after approval", final3["history"][0]["event"] == "docker_completed" and final3["history"][0]["success"])
    finally:
        set_docker_provider(DockerSDKProvider())

    print("\n== 9. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    docker_agent.run_docker_operation("docker_health_check", venture_id="v-test")
    check("published docker_started", "docker_started" in seen_events)
    check("published docker_completed", "docker_completed" in seen_events)

    seen_events.clear()
    set_docker_provider(_FlakyProvider(fail_times=10))
    try:
        docker_agent.run_docker_operation("docker_health_check", venture_id="v-test")
    finally:
        set_docker_provider(DockerSDKProvider())
    check("published docker_failed", "docker_failed" in seen_events)

    print("\n== 10. Manager-callable node shape ==")
    delta = docker_agent.docker_agent_node({"venture_id": "v-test"})
    check("docker_agent_node returns a history delta", "history" in delta and len(delta["history"]) == 1)

    print("\nAll Phase 3 Component 4 checks passed.")


if __name__ == "__main__":
    main()
