from collections.abc import Callable
from typing import Any

from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents.database.chroma.agent import run_chroma_operation
from agents.database.postgres.agent import run_postgres_operation
from agents.founder.ai_builder.agent import run_ai_builder
from agents.infrastructure.docker.agent import run_docker_operation
from agents.infrastructure.n8n.agent import run_n8n_operation
from agents.llm.ollama.agent import run_ollama_operation
from core.event_bus import AFOSEvent, get_event_bus
from core.state import VentureState

# --------------------------------------------------------------------------- #
# Dependency injection: the AI Builder Pipeline is the *only* thing this module
# calls to get a Build Report - never a Tool Registry tool, an LLM, or a raw
# HTTP call of its own. Swappable, not cached, so tests can inject a
# deterministic fake BuildReport-shaped dict instead of exercising the real
# research + decision + planning + build chain. Same convention as every prior
# Phase 4 component's get_x_invoker()/set_x_invoker() pair.
# --------------------------------------------------------------------------- #

AIBuilderInvoker = Callable[[str, str, str], dict[str, Any]]


def _default_ai_builder_invoker(idea: str, venture_id: str, research_depth: str) -> dict[str, Any]:
    return run_ai_builder(idea, venture_id, research_depth)


_ai_builder_invoker: AIBuilderInvoker = _default_ai_builder_invoker


def get_ai_builder_invoker() -> AIBuilderInvoker:
    return _ai_builder_invoker


def set_ai_builder_invoker(fn: AIBuilderInvoker) -> None:
    global _ai_builder_invoker
    _ai_builder_invoker = fn


def reset_ai_builder_invoker() -> None:
    global _ai_builder_invoker
    _ai_builder_invoker = _default_ai_builder_invoker


# --------------------------------------------------------------------------- #
# Dependency injection for each of the 5 reused Phase 3 Infrastructure Agents'
# operation dispatchers - lets tests inject a deterministic fake
# run_x_operation-shaped callable per stage (any subset) instead of exercising
# the real, network/service-dependent Docker/PostgreSQL/Chroma/Ollama/n8n
# backends. Same convention as every prior component's get_x/set_x pair,
# generalized here to one seam per orchestrated agent.
# --------------------------------------------------------------------------- #

InfraOperationFn = Callable[..., dict[str, Any]]

_DEFAULT_INFRA_OPERATIONS: dict[str, InfraOperationFn] = {
    "docker": run_docker_operation,
    "postgres": run_postgres_operation,
    "chroma": run_chroma_operation,
    "ollama": run_ollama_operation,
    "n8n": run_n8n_operation,
}

_infra_operations: dict[str, InfraOperationFn] = dict(_DEFAULT_INFRA_OPERATIONS)


def get_infra_operations() -> dict[str, InfraOperationFn]:
    return _infra_operations


def set_infra_operations(overrides: dict[str, InfraOperationFn]) -> None:
    """Swappable, not cached - lets tests inject deterministic fake operation
    dispatchers for any subset of the 5 reused Infrastructure Agents. Unspecified
    agents keep their default (real) dispatcher.
    """
    global _infra_operations
    _infra_operations = {**_DEFAULT_INFRA_OPERATIONS, **overrides}


def reset_infra_operations() -> None:
    global _infra_operations
    _infra_operations = dict(_DEFAULT_INFRA_OPERATIONS)


class DeploymentPipelineState(VentureState, total=False):
    """Extends VentureState - per its own docstring ("Extend this, never create a
    parallel state shape") - with the fields this workflow needs.
    """

    research_depth: str
    status: str
    error: str
    build_report: dict[str, Any]
    docker_result: dict[str, Any]
    postgres_result: dict[str, Any]
    chroma_result: dict[str, Any]
    ollama_result: dict[str, Any]
    n8n_result: dict[str, Any]
    deployment_report: dict[str, Any]


def _ok(entry: dict[str, Any]) -> bool:
    return str(entry.get("event", "")).endswith("_completed")


def _run_stage(agent_key: str, operations: list[tuple[str, dict[str, Any]]], venture_id: str) -> dict[str, Any]:
    """Runs a fixed sequence of operations against one reused Infrastructure
    Agent's dispatcher (get_infra_operations()[agent_key]), stopping at the
    first operation that fails - never raising (GraphBubbleUp aside): an
    unreachable/misconfigured backend degrades this one stage's result, it never
    aborts the rest of the deployment pipeline.
    """
    fn = get_infra_operations()[agent_key]
    healthy = False
    prepared = False
    details: dict[str, Any] = {}
    error = ""

    for index, (operation, kwargs) in enumerate(operations):
        try:
            result = fn(operation, venture_id=venture_id, **kwargs)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            result = {"event": f"{agent_key}_failed", "error": str(exc)}

        ok = _ok(result)
        if index == 0:
            healthy = ok
        if not ok:
            error = str(result.get("error", f"{agent_key} operation '{operation}' did not complete"))
            details[operation] = result
            break
        details[operation] = result
    else:
        prepared = True

    return {"stage": agent_key, "healthy": healthy, "prepared": prepared, "details": details, "error": error}


def _validate_inputs(state: DeploymentPipelineState) -> str | None:
    """research_depth is not validated here: run_deployment_pipeline() (agents/
    founder/deployment_pipeline/agent.py) already coerces any value outside
    {"standard", "deep"} to "standard" before this graph ever runs - the same
    silent-coercion convention used by every prior Phase 4 component for the
    same field.
    """
    idea = (state.get("idea") or "").strip()
    if not idea:
        return "idea must not be empty"
    venture_id = (state.get("venture_id") or "").strip()
    if not venture_id:
        return "venture_id is required"
    return None


def intake_node(state: DeploymentPipelineState) -> dict:
    bus = get_event_bus()
    venture_id = state.get("venture_id", "")
    idea = state.get("idea", "")

    bus.publish(AFOSEvent(type="deployment_started", source_agent="deployment_pipeline", venture_id=venture_id, payload={"idea": idea}))

    error = _validate_inputs(state)
    if error:
        return {"status": "rejected", "error": error}
    return {"status": "validated"}


def _route_after_intake(state: DeploymentPipelineState) -> str:
    return "build_report" if state.get("status") == "validated" else "aggregate"


def build_report_node(state: DeploymentPipelineState) -> dict:
    """The only place this workflow ever reaches outside itself for a plan to
    deploy - only through the already-completed AI Builder Pipeline's own
    public function, never a Tool Registry tool, LLM, or raw external call
    directly. This *is* requirement #1 ("Receive Build Report").
    """
    try:
        report = get_ai_builder_invoker()(
            state.get("idea", ""), state.get("venture_id", ""), state.get("research_depth") or "standard",
        )
        return {"build_report": report, "status": "build_received"}
    except GraphBubbleUp:
        raise
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


def _route_after_build_report(state: DeploymentPipelineState) -> str:
    """Requirement #2 ("Validate build success"): a Build Report whose status
    isn't "completed" (rejected/failed/build_failed/skipped from AI Builder's
    own gates) never reaches the infrastructure preparation stages.
    """
    if state.get("status") != "build_received":
        return "aggregate"
    if state.get("build_report", {}).get("status") != "completed":
        return "aggregate"
    return "docker_stage"


def docker_stage_node(state: DeploymentPipelineState) -> dict:
    """Requirement #3 ("Prepare Docker environment"). Both operations are
    low/medium risk under the Approval Framework's default policy (min_risk_for_
    approval="high"), so this never pauses for human approval - it auto-approves,
    the same as every other read/provisioning-only operation across Phase 3.
    """
    venture_id = state.get("venture_id", "")
    result = _run_stage(
        "docker",
        [
            ("docker_health_check", {}),
            ("docker_pull_image", {"image_ref": "python:3.11-slim"}),
            ("docker_create_container", {"image_ref": "python:3.11-slim", "container_name": f"{venture_id}-app"}),
        ],
        venture_id,
    )
    return {"docker_result": result}


def postgres_stage_node(state: DeploymentPipelineState) -> dict:
    """Requirement #4 ("Prepare PostgreSQL")."""
    venture_id = state.get("venture_id", "")
    result = _run_stage(
        "postgres",
        [
            ("postgres_health_check", {}),
            ("postgres_create_table", {"table_name": "venture_data", "columns": {"id": "SERIAL PRIMARY KEY", "data": "JSONB"}}),
        ],
        venture_id,
    )
    return {"postgres_result": result}


def chroma_stage_node(state: DeploymentPipelineState) -> dict:
    """Requirement #5 ("Prepare Chroma collections")."""
    venture_id = state.get("venture_id", "")
    result = _run_stage(
        "chroma",
        [
            ("chroma_health_check", {}),
            ("chroma_create_collection", {"collection_name": f"{venture_id}-knowledge"}),
        ],
        venture_id,
    )
    return {"chroma_result": result}


def ollama_stage_node(state: DeploymentPipelineState) -> dict:
    """Requirement #6 ("Verify Ollama availability") - deliberately read-only
    (health check + model listing), matching the "verify" (not "prepare")
    wording.
    """
    venture_id = state.get("venture_id", "")
    result = _run_stage(
        "ollama",
        [
            ("ollama_health_check", {}),
            ("ollama_list_models", {}),
        ],
        venture_id,
    )
    return {"ollama_result": result}


def n8n_stage_node(state: DeploymentPipelineState) -> dict:
    """Requirement #7 ("Prepare n8n workflows")."""
    venture_id = state.get("venture_id", "")
    idea = state.get("idea", "")
    result = _run_stage(
        "n8n",
        [
            ("n8n_health_check", {}),
            ("n8n_create_workflow", {"workflow_name": f"deployment-notify-{venture_id}", "nodes": [{"type": "start"}], "connections": {}}),
        ],
        venture_id,
    )
    return {"n8n_result": result}


def aggregate_node(state: DeploymentPipelineState) -> dict:
    """Requirement #8 ("Produce Deployment Report")."""
    bus = get_event_bus()
    venture_id = state.get("venture_id", "")
    idea = state.get("idea", "")
    status = state.get("status", "failed")

    if status in ("rejected", "failed"):
        reason = state.get("error", "unexpected failure")
        report = {
            "idea": idea, "status": status, "build_validated": False, "stages": [],
            "ready_stage_count": 0, "total_stage_count": 0, "summary": reason, "error": reason,
        }
        bus.publish(AFOSEvent(type="deployment_failed", source_agent="deployment_pipeline", venture_id=venture_id, payload={"reason": reason, "status": status}))
        return {"deployment_report": report, "history": [{"agent": "deployment_pipeline", "event": "deployment_failed", "status": status}]}

    build_report = state.get("build_report", {})
    if build_report.get("status") != "completed":
        reason = f"Build was not successful (status='{build_report.get('status', 'unknown')}') - deployment skipped"
        report = {
            "idea": idea, "status": "skipped", "build_validated": False, "stages": [],
            "ready_stage_count": 0, "total_stage_count": 0, "summary": reason, "error": reason,
        }
        bus.publish(AFOSEvent(type="deployment_failed", source_agent="deployment_pipeline", venture_id=venture_id, payload={"reason": reason, "status": "skipped"}))
        return {"deployment_report": report, "history": [{"agent": "deployment_pipeline", "event": "deployment_failed", "status": "skipped"}]}

    stages = [
        state.get("docker_result", {"stage": "docker", "healthy": False, "prepared": False, "error": "not run"}),
        state.get("postgres_result", {"stage": "postgres", "healthy": False, "prepared": False, "error": "not run"}),
        state.get("chroma_result", {"stage": "chroma", "healthy": False, "prepared": False, "error": "not run"}),
        state.get("ollama_result", {"stage": "ollama", "healthy": False, "prepared": False, "error": "not run"}),
        state.get("n8n_result", {"stage": "n8n", "healthy": False, "prepared": False, "error": "not run"}),
    ]
    ready_count = sum(1 for s in stages if s.get("prepared"))
    total = len(stages)
    final_status = "completed" if ready_count == total else ("partial" if ready_count > 0 else "failed")

    report = {
        "idea": idea,
        "status": final_status,
        "build_validated": True,
        "stages": stages,
        "ready_stage_count": ready_count,
        "total_stage_count": total,
        "summary": f"Deployment for '{idea}': {ready_count}/{total} infrastructure stages ready.",
        "error": "" if final_status == "completed" else "; ".join(s["error"] for s in stages if s.get("error")),
    }

    if final_status == "completed":
        bus.publish(AFOSEvent(type="deployment_completed", source_agent="deployment_pipeline", venture_id=venture_id, payload={"ready_stage_count": ready_count, "total_stage_count": total}))
        return {"deployment_report": report, "history": [{"agent": "deployment_pipeline", "event": "deployment_completed", "status": final_status}]}

    bus.publish(AFOSEvent(type="deployment_failed", source_agent="deployment_pipeline", venture_id=venture_id, payload={"reason": report["error"], "status": final_status}))
    return {"deployment_report": report, "history": [{"agent": "deployment_pipeline", "event": "deployment_failed", "status": final_status}]}


def build_deployment_pipeline() -> StateGraph:
    """Build Report -> Validate -> Docker -> PostgreSQL -> Chroma -> Ollama ->
    n8n -> Deployment Report. Invalid input, a failed Build Report fetch, or a
    Build Report that isn't successful (status != "completed") all short-circuit
    straight to "aggregate" with a well-formed, explanatory Deployment Report -
    never attempting any infrastructure preparation. Each infrastructure stage
    always runs regardless of a prior stage's outcome - one unreachable backend
    (the common case in an unconfigured sandbox) degrades only that stage's
    result, never aborts the pipeline, matching Component 2's Research
    Pipeline's own "gracefully continue if one stage fails" precedent.
    """
    graph = StateGraph(DeploymentPipelineState)
    graph.add_node("intake", intake_node)
    graph.add_node("build_report", build_report_node)
    graph.add_node("docker_stage", docker_stage_node)
    graph.add_node("postgres_stage", postgres_stage_node)
    graph.add_node("chroma_stage", chroma_stage_node)
    graph.add_node("ollama_stage", ollama_stage_node)
    graph.add_node("n8n_stage", n8n_stage_node)
    graph.add_node("aggregate", aggregate_node)

    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", _route_after_intake, ["build_report", "aggregate"])
    graph.add_conditional_edges("build_report", _route_after_build_report, ["docker_stage", "aggregate"])
    graph.add_edge("docker_stage", "postgres_stage")
    graph.add_edge("postgres_stage", "chroma_stage")
    graph.add_edge("chroma_stage", "ollama_stage")
    graph.add_edge("ollama_stage", "n8n_stage")
    graph.add_edge("n8n_stage", "aggregate")
    graph.add_edge("aggregate", END)
    return graph


def compile_deployment_pipeline() -> CompiledStateGraph:
    # Deliberately compiled without a checkpointer, same precedent as
    # workflows/ai_builder.py's compile_ai_builder() - this workflow runs
    # straight through with no approval gate of its own (the reused
    # Infrastructure Agents own their own approval gates internally, unchanged,
    # and every operation used here is low/medium risk so none of them pause).
    return build_deployment_pipeline().compile()
