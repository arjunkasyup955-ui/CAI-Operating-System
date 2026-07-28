import sys
from collections.abc import Callable
from typing import Any

from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents.coding.build_runner.agent import run_build_operation
from agents.coding.claude_code.agent import run_claude_code_delegation
from agents.coding.claude_code.providers import ClaudeCodeTaskRequest
from agents.coding.debug_retry.agent import run_debug_retry
from agents.coding.file_editor.agent import run_file_operation
from agents.coding.git.agent import run_git_operation
from agents.coding.loop_controller.agent import start_loop, step_loop
from agents.founder.mvp_planner.agent import run_mvp_planner
from core.event_bus import AFOSEvent, get_event_bus
from core.state import VentureState
from tools.loop.loop_providers import LoopProvider, LoopStepResult, get_loop_provider, set_loop_provider

# --------------------------------------------------------------------------- #
# Dependency injection: the MVP Planner is the *only* thing this module calls
# to get a plan to build - never a Tool Registry tool, an LLM, or a raw HTTP
# call of its own. Swappable, not cached, so tests can inject a deterministic
# fake MVPPlan-shaped dict instead of exercising the real research + decision +
# planning chain. Same convention as every prior Phase 4 component's
# get_x_invoker()/set_x_invoker() pair.
# --------------------------------------------------------------------------- #

MVPPlannerInvoker = Callable[[str, str, str], dict[str, Any]]


def _default_mvp_planner_invoker(idea: str, venture_id: str, research_depth: str) -> dict[str, Any]:
    return run_mvp_planner(idea, venture_id, research_depth)


_mvp_planner_invoker: MVPPlannerInvoker = _default_mvp_planner_invoker


def get_mvp_planner_invoker() -> MVPPlannerInvoker:
    return _mvp_planner_invoker


def set_mvp_planner_invoker(fn: MVPPlannerInvoker) -> None:
    global _mvp_planner_invoker
    _mvp_planner_invoker = fn


def reset_mvp_planner_invoker() -> None:
    global _mvp_planner_invoker
    _mvp_planner_invoker = _default_mvp_planner_invoker


class AIBuilderState(VentureState, total=False):
    """Extends VentureState - per its own docstring ("Extend this, never create a
    parallel state shape") - with the fields this workflow needs.
    """

    research_depth: str
    status: str
    error: str
    mvp_plan: dict[str, Any]
    execution_plan: list[dict[str, Any]]
    loop_id: str
    loop_result: dict[str, Any]
    debug_retry_count: int
    build_report: dict[str, Any]


_PLANNABLE_MVP_STATUSES = {"completed"}


def _succeeded(entry: dict[str, Any]) -> bool:
    """An entry's event always ends in '_completed' whenever the underlying
    Phase 2 agent call didn't raise - even when the operation itself was a
    semantic no-op (e.g. file_editor_agent's create_file returns event=
    "file_edit_completed" with success=False if the path already exists,
    same convention in git_agent/terminal_agent/build_runner_agent/
    claude_code_agent). Every one of those agents also spreads the
    underlying tool result - including its own "success" field - into the
    returned entry, so prefer that real semantic outcome when present
    instead of trusting the event name alone.
    """
    if not str(entry.get("event", "")).endswith("_completed"):
        return False
    success = entry.get("success")
    return bool(success) if success is not None else True


def _debug_retry_succeeded(entry: dict[str, Any]) -> bool:
    return entry.get("event") == "debug_completed" and entry.get("retried") is True and entry.get("retry_budget_exhausted") is False


class AIBuilderLoopProvider:
    """A custom LoopProvider (the exact extension seam
    tools/loop/loop_providers.py's own docstring describes: "real work would be
    wired in via a custom provider... swapped in through the same DI seam") that
    dispatches each execution-plan step to the already-existing Phase 2 Coding
    Engine agent for that step's type. Duplicates none of their logic - every
    branch below is a single call into an unmodified Phase 2 agent function.
    Also implements requirement #7 ("Automatically invoke Debug & Retry when
    needed"): a failed "build" step is handed to the existing Debug & Retry
    Agent, which diagnoses and (for transient failures) retries the build
    itself before this provider reports success/failure back to the Loop
    Controller's own step-level retry_budget (requirement #8's outer safety net).
    """

    name = "ai_builder"

    def __init__(self, venture_id: str) -> None:
        self._venture_id = venture_id
        self.debug_retry_invocations = 0

    def execute_step(self, step: dict[str, Any], step_index: int) -> LoopStepResult:
        step_type = step.get("type")
        try:
            if step_type == "claude_code":
                request = ClaudeCodeTaskRequest(task_description=step.get("task_description", ""), venture_id=self._venture_id)
                result = run_claude_code_delegation(request)
            elif step_type == "git":
                result = run_git_operation(step["operation"], venture_id=self._venture_id, **step.get("kwargs", {}))
            elif step_type == "file_edit":
                result = run_file_operation(step["operation"], venture_id=self._venture_id, **step.get("kwargs", {}))
            elif step_type == "terminal":
                result = run_terminal_command_step(step, self._venture_id)
            elif step_type == "build":
                result = self._execute_build_step(step)
            else:
                return LoopStepResult(success=False, step_index=step_index, error=f"unknown step type '{step_type}'")
        except Exception as exc:
            return LoopStepResult(success=False, step_index=step_index, error=str(exc))

        success = _succeeded(result)
        return LoopStepResult(
            success=success, step_index=step_index,
            output=result.get("event", "") if success else "",
            error="" if success else str(result.get("error") or f"step {step_index} ({step_type}) did not complete"),
        )

    def _execute_build_step(self, step: dict[str, Any]) -> dict[str, Any]:
        build_type = step["build_type"]
        kwargs = step.get("kwargs", {})
        result = run_build_operation(build_type, venture_id=self._venture_id, **kwargs)
        if _succeeded(result):
            return result

        self.debug_retry_invocations += 1
        debug_entry = run_debug_retry(
            operation="build",
            retry_fn=lambda: run_build_operation(build_type, venture_id=self._venture_id, **kwargs),
            initial_error=str(result.get("error", "build failed")),
            venture_id=self._venture_id,
        )
        if _debug_retry_succeeded(debug_entry):
            return {"event": "build_completed", "debug_retry": debug_entry}
        return {"event": "build_failed", "error": debug_entry.get("error") or debug_entry.get("diagnosis") or "debug/retry did not resolve the build failure", "debug_retry": debug_entry}


StepExecutorFactory = Callable[[str], LoopProvider]


def _default_step_executor_factory(venture_id: str) -> LoopProvider:
    return AIBuilderLoopProvider(venture_id)


_step_executor_factory: StepExecutorFactory = _default_step_executor_factory


def get_step_executor_factory() -> StepExecutorFactory:
    return _step_executor_factory


def set_step_executor_factory(fn: StepExecutorFactory) -> None:
    """Swappable, not cached - lets tests inject a deterministic fake LoopProvider
    (one that touches no real git/filesystem/LLM) instead of exercising
    AIBuilderLoopProvider's real dispatch into the Phase 2 Coding Engine agents.
    Same dependency-injection convention as every other seam in this project.
    """
    global _step_executor_factory
    _step_executor_factory = fn


def reset_step_executor_factory() -> None:
    global _step_executor_factory
    _step_executor_factory = _default_step_executor_factory


def run_terminal_command_step(step: dict[str, Any], venture_id: str) -> dict[str, Any]:
    """Split out as its own function (not inlined in execute_step) purely so a
    dependency-injected fake step function - see tests - never needs to know
    about run_terminal_command's own signature quirks.
    """
    from agents.coding.terminal.agent import run_terminal_command

    return run_terminal_command(step.get("command", []), venture_id=venture_id, timeout_seconds=step.get("timeout_seconds", 30.0))


def _generate_execution_plan(mvp_plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure Python, deterministic mapping from the (already-completed) MVP
    Planner's MVPPlan onto a concrete, ordered list of Loop Controller steps -
    no AI/LLM call, no external API call. The same MVPPlan always produces the
    identical execution plan.
    """
    idea = mvp_plan.get("idea", "")
    steps: list[dict[str, Any]] = []

    for path in mvp_plan.get("folder_structure", []):
        if path.endswith("/"):
            steps.append({"type": "file_edit", "operation": "create_directory", "kwargs": {"path": path}})
        else:
            steps.append(
                {
                    "type": "file_edit", "operation": "create_file",
                    "kwargs": {"path": path, "content": f"# {path}\n# Scaffolded by the AI Builder Pipeline for: {idea}\n"},
                }
            )

    feature_names = [f.get("name", "") for f in mvp_plan.get("feature_list", [])]
    steps.append(
        {
            "type": "claude_code",
            "task_description": (
                f"Implement the following MVP features for '{idea}': {', '.join(feature_names) or 'the core workflow'}. "
                f"Follow the recommended tech stack and API outline from the MVP plan."
            ),
        }
    )

    steps.append({"type": "git", "operation": "add", "kwargs": {"paths": ["."]}})
    steps.append({"type": "git", "operation": "commit", "kwargs": {"message": f"AI Builder: initial scaffold for {idea}"}})

    steps.append({"type": "terminal", "command": [sys.executable, "--version"]})
    steps.append({"type": "build", "build_type": "python", "kwargs": {"action": "test"}})

    return steps


def _validate_inputs(state: AIBuilderState) -> str | None:
    """research_depth is not validated here: run_ai_builder() (agents/founder/
    ai_builder/agent.py) already coerces any value outside {"standard", "deep"}
    to "standard" before this graph ever runs - the same silent-coercion
    convention used by every prior Phase 4 component for the same field.
    """
    idea = (state.get("idea") or "").strip()
    if not idea:
        return "idea must not be empty"
    venture_id = (state.get("venture_id") or "").strip()
    if not venture_id:
        return "venture_id is required"
    return None


def intake_node(state: AIBuilderState) -> dict:
    bus = get_event_bus()
    venture_id = state.get("venture_id", "")
    idea = state.get("idea", "")

    bus.publish(AFOSEvent(type="ai_builder_started", source_agent="ai_builder", venture_id=venture_id, payload={"idea": idea}))

    error = _validate_inputs(state)
    if error:
        return {"status": "rejected", "error": error}
    return {"status": "validated"}


def _route_after_intake(state: AIBuilderState) -> str:
    return "mvp_plan" if state.get("status") == "validated" else "aggregate"


def mvp_plan_node(state: AIBuilderState) -> dict:
    """The Founder Decision Engine/Research Pipeline chain is never reached from
    here directly - only through the already-completed MVP Planner's own public
    function (see get_mvp_planner_invoker()), which is itself the only thing
    this workflow "consumes" per this component's explicit requirement.
    """
    try:
        plan = get_mvp_planner_invoker()(
            state.get("idea", ""), state.get("venture_id", ""), state.get("research_depth") or "standard",
        )
        return {"mvp_plan": plan, "status": "planned"}
    except GraphBubbleUp:
        raise
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


def _route_after_mvp_plan(state: AIBuilderState) -> str:
    if state.get("status") != "planned":
        return "aggregate"
    mvp_plan = state.get("mvp_plan", {})
    if mvp_plan.get("status") not in _PLANNABLE_MVP_STATUSES:
        return "aggregate"
    return "generate_execution_plan"


def generate_execution_plan_node(state: AIBuilderState) -> dict:
    execution_plan = _generate_execution_plan(state.get("mvp_plan", {}))
    return {"execution_plan": execution_plan, "status": "plan_generated"}


def build_loop_node(state: AIBuilderState) -> dict:
    """Drives the reused Loop Controller Agent to completion. Loop Controller's
    own design deliberately makes "the loop" external - repeated step_loop()
    calls on the same thread_id, never a self-edge inside its own graph (see
    agents/coding/loop_controller/agent.py's _loop_step_node docstring) - so
    this while-loop IS that external driver, not a duplicate of Loop
    Controller's own pause/resume/cancel/retry-budget/max-iteration/timeout
    logic, all of which is reused entirely unmodified via start_loop()/
    step_loop().
    """
    venture_id = state.get("venture_id", "")
    steps = state.get("execution_plan", [])

    original_provider: LoopProvider = get_loop_provider()
    provider = get_step_executor_factory()(venture_id)
    set_loop_provider(provider)
    try:
        result = start_loop(steps=steps, venture_id=venture_id, max_iterations=len(steps) * 4 + 5, timeout_seconds=180.0, retry_budget=2)
        loop_id = result.get("loop_id", "")
        while result.get("status") == "running":
            result = step_loop(loop_id, venture_id)
        status = "built" if result.get("status") == "completed" else "build_failed"
        return {"loop_id": loop_id, "loop_result": result, "debug_retry_count": getattr(provider, "debug_retry_invocations", 0), "status": status}
    except GraphBubbleUp:
        raise
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}
    finally:
        set_loop_provider(original_provider)


def aggregate_node(state: AIBuilderState) -> dict:
    bus = get_event_bus()
    venture_id = state.get("venture_id", "")
    idea = state.get("idea", "")
    status = state.get("status", "failed")

    if status == "built":
        loop_result = state.get("loop_result", {})
        progress = loop_result.get("progress", [])
        report = {
            "idea": idea,
            "status": "completed",
            "loop_status": loop_result.get("status", "unknown"),
            "steps_total": len(state.get("execution_plan", [])),
            "steps_completed": loop_result.get("current_step_index", 0),
            "iteration_count": loop_result.get("iteration_count", 0),
            "debug_retry_invocations": state.get("debug_retry_count", 0),
            "step_progress": progress,
            "summary": f"AI Builder completed '{idea}' - {loop_result.get('current_step_index', 0)}/{len(state.get('execution_plan', []))} steps executed.",
            "error": "",
        }
        bus.publish(AFOSEvent(type="ai_builder_completed", source_agent="ai_builder", venture_id=venture_id, payload={"steps_completed": report["steps_completed"], "steps_total": report["steps_total"]}))
        return {"build_report": report, "history": [{"agent": "ai_builder", "event": "ai_builder_completed", "status": "completed"}]}

    if status == "rejected":
        reason = state.get("error", "invalid input")
    elif status == "failed":
        reason = state.get("error", "unexpected failure")
    elif status == "build_failed":
        loop_result = state.get("loop_result", {})
        reason = f"build loop failed: {loop_result.get('error', 'unknown error')}"
    else:
        mvp_plan = state.get("mvp_plan", {})
        reason = f"MVP Planner did not produce a buildable plan (status='{mvp_plan.get('status', 'unknown')}') - build skipped"

    report = {
        "idea": idea,
        "status": status if status in ("rejected", "failed", "build_failed") else "skipped",
        "loop_status": state.get("loop_result", {}).get("status", "not_started"),
        "steps_total": len(state.get("execution_plan", [])),
        "steps_completed": state.get("loop_result", {}).get("current_step_index", 0),
        "iteration_count": state.get("loop_result", {}).get("iteration_count", 0),
        "debug_retry_invocations": state.get("debug_retry_count", 0),
        "step_progress": state.get("loop_result", {}).get("progress", []),
        "summary": reason,
        "error": reason,
    }
    bus.publish(AFOSEvent(type="ai_builder_failed", source_agent="ai_builder", venture_id=venture_id, payload={"reason": reason, "status": report["status"]}))
    return {"build_report": report, "history": [{"agent": "ai_builder", "event": "ai_builder_failed", "status": report["status"]}]}


def build_ai_builder() -> StateGraph:
    """MVP Plan -> Generate Execution Plan -> Delegate/Edit/Execute/Build via the
    Loop Controller (dispatching to Claude Code/Git/File Editor/Terminal/Build
    Runner, with Debug & Retry automatically invoked on build failure) ->
    Aggregate into a Build Report. Invalid input, a failed MVP Plan fetch, or an
    MVP Plan that isn't buildable (status != "completed", e.g. "not_recommended"
    from Component 4's own PIVOT/DROP gate) all short-circuit straight to
    "aggregate" with a well-formed, explanatory Build Report - never a
    partially-populated one, and never attempting a build.
    """
    graph = StateGraph(AIBuilderState)
    graph.add_node("intake", intake_node)
    graph.add_node("mvp_plan", mvp_plan_node)
    graph.add_node("generate_execution_plan", generate_execution_plan_node)
    graph.add_node("build_loop", build_loop_node)
    graph.add_node("aggregate", aggregate_node)

    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", _route_after_intake, ["mvp_plan", "aggregate"])
    graph.add_conditional_edges("mvp_plan", _route_after_mvp_plan, ["generate_execution_plan", "aggregate"])
    graph.add_edge("generate_execution_plan", "build_loop")
    graph.add_edge("build_loop", "aggregate")
    graph.add_edge("aggregate", END)
    return graph


def compile_ai_builder() -> CompiledStateGraph:
    # Deliberately compiled without a checkpointer, same precedent as
    # workflows/mvp_planner.py's compile_mvp_planner() - this workflow runs
    # straight through with no approval gate of its own (the reused Coding
    # Engine agents own their own approval gates internally, unchanged).
    return build_ai_builder().compile()
