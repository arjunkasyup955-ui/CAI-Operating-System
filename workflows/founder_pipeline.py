import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from core.approval import ApprovalRequest, get_approval_engine
from core.event_bus import AFOSEvent, get_event_bus
from core.state import VentureState
from workflows.research_graph import compile_research_graph

logger = logging.getLogger("afos.workflows.founder_pipeline")

_research_graph = compile_research_graph()

ResearchInvoker = Callable[[dict[str, Any]], dict[str, Any]]


def _default_research_invoker(payload: dict[str, Any]) -> dict[str, Any]:
    """Invokes the existing, already-verified Research Supervisor subgraph
    statelessly - the identical technique workflows/main_graph.py's
    research_supervisor_node already uses, to avoid double-accumulating
    Annotated[list, operator.add] fields across the subgraph boundary. This single
    call runs all 8 Phase 1 research workers (research_supervisor, search, browser,
    market/competitor/trend intelligence, opportunity_detection, idea_validation) in
    the fixed order wired by workflows/research_graph.py - none of them are modified
    or re-implemented here.
    """
    return _research_graph.invoke(payload)


_research_invoker: ResearchInvoker = _default_research_invoker


def get_research_invoker() -> ResearchInvoker:
    return _research_invoker


def set_research_invoker(fn: ResearchInvoker) -> None:
    """Swappable, not cached - lets tests inject a deterministic/flaky/slow research
    invoker instead of exercising the real (network- and LLM-dependent) research
    subgraph. Same dependency-injection convention as every Phase 2/3 provider's
    get_x_provider()/set_x_provider() pair.
    """
    global _research_invoker
    _research_invoker = fn


class FounderPipelineState(VentureState, total=False):
    """Extends VentureState - per its own docstring ("Extend this, never create a
    parallel state shape") - with the additional fields the founder-level pipeline
    needs. Nothing here removes or repurposes an existing VentureState field.
    """

    constraints: dict[str, Any]
    budget: float | None
    target_market: str
    analysis_tasks: list[str]
    decision_tasks: list[str]
    risk_level: str
    status: str
    error: str
    execution_plan: dict[str, Any]


_RESEARCH_TASK_AGENTS = {
    "research_supervisor", "search_agent", "browser_agent",
    "market_intelligence_agent", "competitor_intelligence_agent", "trend_intelligence_agent",
}
_ANALYSIS_TASK_AGENTS = {"opportunity_detection_agent"}
_DECISION_TASK_AGENTS = {"idea_validation_agent"}
_EXECUTION_ORDER = [
    "research_supervisor", "search_agent", "browser_agent", "market_intelligence_agent",
    "competitor_intelligence_agent", "trend_intelligence_agent", "opportunity_detection_agent", "idea_validation_agent",
]

# Module-level, overridable in tests - real defaults are conservative; the smoke
# test lowers these to keep the deliberately-slow-timeout scenario fast.
_HIGH_BUDGET_APPROVAL_THRESHOLD_USD = 1000.0
_RESEARCH_TIMEOUT_SECONDS = 300.0
_RESEARCH_MAX_ATTEMPTS = 2
_RESEARCH_BACKOFF_SECONDS = 0.5

# Duplicate-execution guard: a venture_id currently mid-pipeline (running or
# paused awaiting approval) cannot be started again until it reaches plan_node.
_in_progress: set[str] = set()


def _validate_inputs(state: FounderPipelineState) -> str | None:
    """Returns an error message if inputs are invalid, else None. Covers every
    named graceful-failure scenario that's a pure input problem (not a runtime
    failure), checked before any agent runs.
    """
    idea = (state.get("idea") or "").strip()
    if not idea:
        return "idea must not be empty"
    venture_id = (state.get("venture_id") or "").strip()
    if not venture_id:
        return "venture_id is required"
    constraints = state.get("constraints")
    if constraints is not None and not isinstance(constraints, dict):
        return "constraints must be a dict if provided"
    budget = state.get("budget")
    if budget is not None and (not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget < 0):
        return "budget must be a non-negative number if provided"
    return None


def intake_node(state: FounderPipelineState) -> dict:
    """Validates inputs, guards against duplicate concurrent execution for the same
    venture_id, and gates pipeline kickoff through the existing Approval Framework -
    auto-approved at low risk (the common case), genuinely interrupting for a human
    decision when the requested budget crosses the high-risk threshold.
    """
    bus = get_event_bus()
    venture_id = state.get("venture_id", "")
    idea = state.get("idea", "")

    bus.publish(
        AFOSEvent(type="founder_pipeline_started", source_agent="founder_orchestrator", venture_id=venture_id, payload={"idea": idea})
    )

    error = _validate_inputs(state)
    if error:
        bus.publish(
            AFOSEvent(type="founder_pipeline_rejected", source_agent="founder_orchestrator", venture_id=venture_id, payload={"reason": error})
        )
        return {"status": "rejected", "error": error, "history": [{"agent": "founder_orchestrator", "event": "founder_pipeline_rejected", "reason": error}]}

    if venture_id in _in_progress:
        reason = f"duplicate execution: a founder pipeline is already running for venture_id '{venture_id}'"
        bus.publish(
            AFOSEvent(type="founder_pipeline_rejected", source_agent="founder_orchestrator", venture_id=venture_id, payload={"reason": reason})
        )
        return {"status": "rejected", "error": reason, "history": [{"agent": "founder_orchestrator", "event": "founder_pipeline_rejected", "reason": reason}]}

    budget = state.get("budget")
    risk_level = "high" if budget and budget >= _HIGH_BUDGET_APPROVAL_THRESHOLD_USD else "low"
    decision = get_approval_engine().request(
        ApprovalRequest(
            action="founder_pipeline_start", venture_id=venture_id, risk_level=risk_level,
            amount_usd=budget or 0.0, details={"idea": idea, "budget": budget},
        )
    )
    if not decision.approved:
        bus.publish(
            AFOSEvent(type="founder_pipeline_rejected", source_agent="founder_orchestrator", venture_id=venture_id, payload={"reason": decision.reason})
        )
        return {"status": "rejected", "error": decision.reason, "history": [{"agent": "founder_orchestrator", "event": "founder_pipeline_rejected", "reason": decision.reason}]}

    _in_progress.add(venture_id)
    return {"status": "approved", "history": [{"agent": "founder_orchestrator", "event": "founder_pipeline_approved"}]}


def _route_after_intake(state: FounderPipelineState) -> str:
    return "research" if state.get("status") == "approved" else "plan"


_executor = ThreadPoolExecutor(max_workers=4)


def _invoke_research_with_resilience(payload: dict[str, Any]) -> dict[str, Any]:
    """Retry + timeout around the research subgraph invocation, mirroring
    ToolRegistry.invoke()'s own algorithm (ThreadPoolExecutor-based timeout,
    retried with linear backoff) - the orchestrator has no tool of its own to
    register this as, since it coordinates existing agents rather than calling any
    new external system directly.

    Uses one persistent, module-level executor rather than a fresh
    `with ThreadPoolExecutor() as executor:` per call: exiting that context manager
    calls shutdown(wait=True), which blocks until the submitted (possibly still-
    running, timed-out) worker thread finishes - silently defeating the very
    timeout this function exists to enforce. A persistent executor never blocks the
    caller past `_RESEARCH_TIMEOUT_SECONDS`, matching ToolRegistry.invoke()'s own
    behavior exactly (it keeps one executor for the registry's lifetime too).
    """
    last_exc: Exception | None = None
    for attempt in range(1, _RESEARCH_MAX_ATTEMPTS + 1):
        try:
            future = _executor.submit(get_research_invoker(), payload)
            return future.result(timeout=_RESEARCH_TIMEOUT_SECONDS)
        except FutureTimeoutError:
            last_exc = TimeoutError(f"research pipeline timed out after {_RESEARCH_TIMEOUT_SECONDS}s")
            logger.warning("founder_orchestrator: research invocation timed out (attempt %d)", attempt)
        except Exception as exc:
            last_exc = exc
            logger.warning("founder_orchestrator: research invocation failed (attempt %d): %s", attempt, exc)
        if attempt < _RESEARCH_MAX_ATTEMPTS:
            time.sleep(_RESEARCH_BACKOFF_SECONDS * attempt)
    raise last_exc  # type: ignore[misc]


def research_node(state: FounderPipelineState) -> dict:
    """Invokes the existing, unmodified Research Supervisor subgraph (which itself
    already sequences Search, Browser, Market/Competitor/Trend Intelligence,
    Opportunity Detection, and Idea Validation) exactly once per pipeline run, with
    retry+timeout resilience. A worker failing hard inside the subgraph, or the
    whole invocation timing out, is caught here and recorded as a failed status -
    never raised (GraphBubbleUp aside, which must always propagate so a genuine
    human-approval interrupt elsewhere in the process is never swallowed).
    """
    bus = get_event_bus()
    venture_id = state.get("venture_id", "")
    payload = {"idea": state.get("idea", ""), "venture_id": venture_id, "phase": state.get("phase")}

    try:
        result = _invoke_research_with_resilience(payload)
        bus.publish(
            AFOSEvent(type="founder_pipeline_research_completed", source_agent="founder_orchestrator", venture_id=venture_id, payload={})
        )
        return {
            "research_findings": result.get("research_findings", []),
            "history": result.get("history", []),
            "status": "researched",
        }
    except GraphBubbleUp:
        raise
    except Exception as exc:
        logger.warning("founder_orchestrator: research stage failed: %s", exc)
        bus.publish(
            AFOSEvent(type="founder_pipeline_research_failed", source_agent="founder_orchestrator", venture_id=venture_id, payload={"error": str(exc)})
        )
        return {
            "status": "failed",
            "error": str(exc),
            "history": [{"agent": "founder_orchestrator", "event": "founder_pipeline_research_failed", "error": str(exc)}],
        }


def analysis_node(state: FounderPipelineState) -> dict:
    """Classifies findings already produced by the single research_node invocation
    above - does not call any agent itself. Opportunity Detection already ran
    inside the research subgraph; this only extracts and labels its output as this
    pipeline's "Analysis" stage.
    """
    findings = state.get("research_findings", [])
    analysis_tasks = [f"{f.get('event')} ({f.get('agent')})" for f in findings if f.get("agent") in _ANALYSIS_TASK_AGENTS]
    return {"analysis_tasks": analysis_tasks}


def decision_node(state: FounderPipelineState) -> dict:
    """Classifies findings already produced by the single research_node invocation
    above - does not call any agent itself. Idea Validation already ran inside the
    research subgraph; this only extracts and labels its output as this pipeline's
    "Decision" stage, and derives the plan's risk_level from it.
    """
    findings = state.get("research_findings", [])
    decision_tasks = [f"{f.get('event')} ({f.get('agent')})" for f in findings if f.get("agent") in _DECISION_TASK_AGENTS]

    validation_entries = [f for f in findings if f.get("agent") == "idea_validation_agent"]
    if not validation_entries or validation_entries[-1].get("event") != "idea_validation_completed":
        risk_level = "high"
    else:
        score = validation_entries[-1].get("overall_score", 0.0)
        risk_level = "low" if score >= 7.0 else "medium" if score >= 4.0 else "high"

    return {"decision_tasks": decision_tasks, "risk_level": risk_level}


def plan_node(state: FounderPipelineState) -> dict:
    """Assembles the final FounderExecutionPlan and always clears the
    duplicate-execution guard for this venture_id, whether the run completed,
    failed, or was rejected.
    """
    from core.registries import get_agent_registry

    venture_id = state.get("venture_id", "")
    _in_progress.discard(venture_id)

    status = state.get("status", "failed")
    findings = state.get("research_findings", [])
    research_tasks = [f"{f.get('event')} ({f.get('agent')})" for f in findings if f.get("agent") in _RESEARCH_TASK_AGENTS]

    required_agents: list[str] = []
    execution_order: list[str] = []
    if status != "rejected":
        try:
            registry = get_agent_registry()
            required_agents = ["research_supervisor"] + sorted(a.name for a in registry.list_by_supervisor("research_supervisor"))
        except Exception:
            required_agents = []
        execution_order = list(_EXECUTION_ORDER)

    final_status = "completed" if status == "researched" else status
    risk_level = state.get("risk_level", "high")

    # On the rejected-for-invalid-constraints path, state["constraints"] may still
    # hold the caller's malformed (non-dict) value - never let that leak into the
    # plan, which promises a dict.
    raw_constraints = state.get("constraints")
    constraints = raw_constraints if isinstance(raw_constraints, dict) else {}

    raw_budget = state.get("budget")
    budget = raw_budget if isinstance(raw_budget, (int, float)) and not isinstance(raw_budget, bool) else None

    plan = {
        "idea": state.get("idea", ""),
        "venture_id": venture_id,
        "constraints": constraints,
        "budget": budget,
        "target_market": state.get("target_market", ""),
        "research_tasks": research_tasks,
        "analysis_tasks": state.get("analysis_tasks", []),
        "decision_tasks": state.get("decision_tasks", []),
        "required_agents": required_agents,
        "execution_order": execution_order,
        "estimated_runtime_seconds": len(execution_order) * 8.0,
        "risk_level": risk_level,
        "status": final_status,
        "summary": f"Founder pipeline for '{state.get('idea', '')}' finished with status '{final_status}'.",
        "error": state.get("error", ""),
    }

    get_event_bus().publish(
        AFOSEvent(type="founder_pipeline_completed", source_agent="founder_orchestrator", venture_id=venture_id, payload={"status": final_status})
    )

    return {
        "execution_plan": plan,
        "history": [{"agent": "founder_orchestrator", "event": "founder_pipeline_completed", "status": final_status}],
    }


def build_founder_pipeline() -> StateGraph:
    """Idea -> Research -> Analysis -> Decision -> Plan. "Research" invokes the
    existing, unmodified Research Supervisor subgraph exactly once (which itself
    already sequences Search/Browser/Market/Competitor/Trend Intelligence,
    Opportunity Detection, and Idea Validation - a compiled StateGraph can't be
    invoked "partway"). "Analysis" and "Decision" are the founder-level
    classification of that single invocation's output into their respective task
    buckets, not separate agent calls.
    """
    graph = StateGraph(FounderPipelineState)
    graph.add_node("intake", intake_node)
    graph.add_node("research", research_node)
    graph.add_node("analysis", analysis_node)
    graph.add_node("decision", decision_node)
    graph.add_node("plan", plan_node)
    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", _route_after_intake, ["research", "plan"])
    graph.add_edge("research", "analysis")
    graph.add_edge("analysis", "decision")
    graph.add_edge("decision", "plan")
    graph.add_edge("plan", END)
    return graph


def compile_founder_pipeline(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    return build_founder_pipeline().compile(checkpointer=checkpointer)
