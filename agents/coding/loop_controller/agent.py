import logging
import time
import uuid
from pathlib import Path
from typing import Any, TypedDict

from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

import tools.loop.loop  # noqa: F401  (import registers the execute_loop_step tool)
from core.approval import ApprovalRequest, get_approval_engine
from core.event_bus import AFOSEvent, get_event_bus
from core.memory_gateway import get_memory_gateway
from core.registries import get_agent_registry, get_tool_registry
from core.state import VentureState

logger = logging.getLogger("afos.agents.loop_controller")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")


class LoopState(TypedDict, total=False):
    loop_id: str
    venture_id: str
    steps: list[dict[str, Any]]
    current_step_index: int
    status: str  # "running" | "paused" | "cancelled" | "completed" | "failed"
    iteration_count: int
    max_iterations: int
    retry_count: int
    retry_budget: int
    progress: list[dict[str, Any]]
    started_at: float
    timeout_seconds: float
    error: str


def _loop_step_node(state: LoopState) -> dict:
    """Processes exactly one step per graph invocation - "the loop" is external
    (driven by repeated invoke() calls on the same thread_id), not a self-edge inside
    the graph. This is what makes pause/resume/cancel meaningful: nothing is ever
    running "in the background" to interrupt - between any two steps, the persisted
    state can be read or rewritten by pause_loop()/cancel_loop().

    Hard safety limits are checked first, every time, so a loop can never run forever
    regardless of what the steps themselves do.
    """
    if state.get("status") == "cancelled":
        return {}
    if time.time() - state.get("started_at", time.time()) > state.get("timeout_seconds", 60.0):
        return {"status": "failed", "error": "timeout_exceeded"}
    if state.get("iteration_count", 0) >= state.get("max_iterations", 50):
        return {"status": "failed", "error": "max_iterations_exceeded"}

    steps = state.get("steps", [])
    idx = state.get("current_step_index", 0)
    if idx >= len(steps):
        return {"status": "completed"}

    step = steps[idx]

    try:
        result = get_tool_registry().invoke("execute_loop_step", agent_name="loop_controller_agent", step=step, step_index=idx)
    except Exception as exc:
        result = {"success": False, "step_index": idx, "error": str(exc)}

    if result.get("success"):
        return {
            "current_step_index": idx + 1,
            "iteration_count": state.get("iteration_count", 0) + 1,
            "retry_count": 0,
            "status": "running",
            "progress": [{"step_index": idx, "success": True, "output": result.get("output", "")}],
        }

    retry_count = state.get("retry_count", 0)
    retry_budget = state.get("retry_budget", 3)
    if retry_count < retry_budget:
        return {
            "iteration_count": state.get("iteration_count", 0) + 1,
            "retry_count": retry_count + 1,
            "status": "running",
            "progress": [{"step_index": idx, "success": False, "error": result.get("error", ""), "retrying": True}],
        }

    return {
        "status": "failed",
        "error": f"step {idx} failed after exhausting retry budget: {result.get('error', '')}",
    }


def build_loop_graph() -> StateGraph:
    graph = StateGraph(LoopState)
    graph.add_node("step", _loop_step_node)
    graph.add_edge(START, "step")
    graph.add_edge("step", END)
    return graph


def compile_loop_graph(checkpointer) -> CompiledStateGraph:
    return build_loop_graph().compile(checkpointer=checkpointer)


def _config(loop_id: str) -> dict:
    return {"configurable": {"thread_id": loop_id}}


def _publish(event_type: str, loop_id: str, venture_id: str, payload: dict[str, Any]) -> None:
    get_event_bus().publish(
        AFOSEvent(type=event_type, source_agent="loop_controller_agent", venture_id=venture_id, payload={"loop_id": loop_id, **payload})
    )


def _handle_terminal_events(state: dict, loop_id: str, venture_id: str) -> None:
    status = state.get("status")
    if status == "completed":
        _publish("loop_completed", loop_id, venture_id, {"iteration_count": state.get("iteration_count")})
    elif status == "failed":
        _publish("loop_failed", loop_id, venture_id, {"error": state.get("error", "")})
    elif status == "cancelled":
        _publish("loop_cancelled", loop_id, venture_id, {})
    elif status == "running":
        last_progress = (state.get("progress") or [{}])[-1]
        if last_progress.get("retrying"):
            _publish("loop_retry", loop_id, venture_id, {"step_index": last_progress.get("step_index")})
        _publish(
            "loop_progress",
            loop_id,
            venture_id,
            {"current_step_index": state.get("current_step_index"), "iteration_count": state.get("iteration_count")},
        )


def start_loop(
    steps: list[dict[str, Any]],
    venture_id: str = "default",
    max_iterations: int = 50,
    timeout_seconds: float = 60.0,
    retry_budget: int = 3,
) -> dict:
    """Creates a new loop, requests a (low-risk, auto-approving) sign-off to begin,
    and processes exactly the first step. Never raises for a genuine error.
    """
    loop_id = str(uuid.uuid4())
    _publish("loop_started", loop_id, venture_id, {"step_count": len(steps)})

    gateway = get_memory_gateway()
    try:
        decision = get_approval_engine().request(
            ApprovalRequest(action="loop_start", venture_id=venture_id, risk_level="low", details={"step_count": len(steps)})
        )
        if not decision.approved:
            state = {"loop_id": loop_id, "venture_id": venture_id, "status": "failed", "error": f"start denied: {decision.reason}"}
            _handle_terminal_events(state, loop_id, venture_id)
            return state

        with gateway.working() as checkpointer:
            graph = compile_loop_graph(checkpointer)
            initial_state: LoopState = {
                "loop_id": loop_id,
                "venture_id": venture_id,
                "steps": steps,
                "current_step_index": 0,
                "status": "running",
                "iteration_count": 0,
                "max_iterations": max_iterations,
                "retry_count": 0,
                "retry_budget": retry_budget,
                "progress": [],
                "started_at": time.time(),
                "timeout_seconds": timeout_seconds,
                "error": "",
            }
            state = graph.invoke(initial_state, config=_config(loop_id))
            _handle_terminal_events(state, loop_id, venture_id)
            return state
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("loop_controller_agent: start_loop failed: %s", exc)
        state = {"loop_id": loop_id, "venture_id": venture_id, "status": "failed", "error": str(exc)}
        _publish("loop_failed", loop_id, venture_id, {"error": str(exc)})
        return state


def step_loop(loop_id: str, venture_id: str = "default") -> dict:
    """Processes exactly one more step, if the loop is currently running. No-ops
    (returns the current state unchanged) if paused/cancelled/completed/failed.
    """
    gateway = get_memory_gateway()
    try:
        with gateway.working() as checkpointer:
            graph = compile_loop_graph(checkpointer)
            config = _config(loop_id)
            current = graph.get_state(config).values
            if current.get("status") != "running":
                return current

            state = graph.invoke({}, config=config)
            _handle_terminal_events(state, loop_id, venture_id)
            return state
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("loop_controller_agent: step_loop failed for '%s': %s", loop_id, exc)
        state = {"loop_id": loop_id, "venture_id": venture_id, "status": "failed", "error": str(exc)}
        _publish("loop_failed", loop_id, venture_id, {"error": str(exc)})
        return state


def pause_loop(loop_id: str, venture_id: str = "default") -> dict:
    gateway = get_memory_gateway()
    with gateway.working() as checkpointer:
        graph = compile_loop_graph(checkpointer)
        config = _config(loop_id)
        current = graph.get_state(config).values
        if current.get("status") != "running":
            return current
        graph.update_state(config, {"status": "paused"})
        state = graph.get_state(config).values
        _publish("loop_paused", loop_id, venture_id, {"current_step_index": state.get("current_step_index")})
        return state


def resume_loop(loop_id: str, venture_id: str = "default") -> dict:
    """Flips paused -> running, then processes exactly one step."""
    gateway = get_memory_gateway()
    try:
        with gateway.working() as checkpointer:
            graph = compile_loop_graph(checkpointer)
            config = _config(loop_id)
            current = graph.get_state(config).values
            if current.get("status") != "paused":
                return current

            graph.update_state(config, {"status": "running"})
            _publish("loop_resumed", loop_id, venture_id, {"current_step_index": current.get("current_step_index")})

            state = graph.invoke({}, config=config)
            _handle_terminal_events(state, loop_id, venture_id)
            return state
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("loop_controller_agent: resume_loop failed for '%s': %s", loop_id, exc)
        state = {"loop_id": loop_id, "venture_id": venture_id, "status": "failed", "error": str(exc)}
        _publish("loop_failed", loop_id, venture_id, {"error": str(exc)})
        return state


def cancel_loop(loop_id: str, venture_id: str = "default") -> dict:
    """Graceful stop - marks the loop cancelled so no future step_loop()/resume_loop()
    call will ever process another step, regardless of its current status (short of
    an already-terminal one)."""
    gateway = get_memory_gateway()
    with gateway.working() as checkpointer:
        graph = compile_loop_graph(checkpointer)
        config = _config(loop_id)
        current = graph.get_state(config).values
        if current.get("status") in ("completed", "failed", "cancelled"):
            return current
        graph.update_state(config, {"status": "cancelled"})
        state = graph.get_state(config).values
        _publish("loop_cancelled", loop_id, venture_id, {"current_step_index": state.get("current_step_index")})
        return state


def get_loop_status(loop_id: str) -> dict:
    """Read-only - never executes anything, just returns the persisted state."""
    gateway = get_memory_gateway()
    with gateway.working() as checkpointer:
        graph = compile_loop_graph(checkpointer)
        return graph.get_state(_config(loop_id)).values


def loop_controller_node(state: VentureState) -> dict:
    """Manager-callable entry point in the same node shape every other agent uses.
    Not wired into any graph in this component. Runs a trivial 1-step loop to
    completion as a placeholder demonstration.
    """
    result = start_loop(steps=[{"type": "noop"}], venture_id=state.get("venture_id", "default"), max_iterations=5, timeout_seconds=10.0)
    entry = {"agent": "loop_controller_agent", "event": f"loop_{result.get('status', 'unknown')}", **result}
    return {"history": [entry]}
