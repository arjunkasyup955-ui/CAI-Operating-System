import threading
from collections.abc import Callable
from typing import Any

from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents.coding.git.agent import run_git_operation
from agents.founder.ai_builder.agent import run_ai_builder
from agents.founder.decision_engine.agent import run_decision_engine
from agents.founder.deployment_pipeline.agent import run_deployment_pipeline
from agents.founder.growth_pipeline.agent import run_growth_pipeline
from agents.founder.mvp_planner.agent import run_mvp_planner
from agents.founder.orchestrator.agent import run_founder_pipeline
from agents.founder.research_pipeline.agent import run_research_pipeline
from core.event_bus import AFOSEvent, get_event_bus
from core.state import VentureState
from workflows.decision_engine import reset_research_pipeline_invoker, set_research_pipeline_invoker

# --------------------------------------------------------------------------- #
# Dependency injection: the 7 prior Phase 4 components are the *only* things
# this module calls to get data - never a Tool Registry tool, an LLM, or a raw
# HTTP call of its own (Git Status is the one exception, reusing the existing
# Phase 2 Git Agent directly, per this component's own field list). Swappable,
# not cached, so tests can inject deterministic fake report-shaped dicts
# instead of exercising the real, independently-recomputed research/decision/
# planning/build/deployment/growth chains seven times over. Same convention as
# every prior Phase 4 component's get_x_invoker()/set_x_invoker() pair,
# generalized here to one dict covering all 7 reused components.
# --------------------------------------------------------------------------- #

ComponentInvoker = Callable[[str, str, str], dict[str, Any]]


def _default_founder_orchestrator_invoker(idea: str, venture_id: str, research_depth: str) -> dict[str, Any]:
    # Founder Orchestrator's own signature has no research_depth param (it takes
    # constraints/budget/target_market instead) - called here with no budget, so
    # it always resolves to low risk and never pauses for approval.
    return run_founder_pipeline(idea, venture_id)


def _default_research_pipeline_invoker(idea: str, venture_id: str, research_depth: str) -> dict[str, Any]:
    return run_research_pipeline(idea, venture_id, research_depth)


def _default_decision_engine_invoker(idea: str, venture_id: str, research_depth: str) -> dict[str, Any]:
    return run_decision_engine(idea, venture_id, research_depth)


def _default_mvp_planner_invoker(idea: str, venture_id: str, research_depth: str) -> dict[str, Any]:
    return run_mvp_planner(idea, venture_id, research_depth)


def _default_ai_builder_invoker(idea: str, venture_id: str, research_depth: str) -> dict[str, Any]:
    return run_ai_builder(idea, venture_id, research_depth)


def _default_deployment_pipeline_invoker(idea: str, venture_id: str, research_depth: str) -> dict[str, Any]:
    return run_deployment_pipeline(idea, venture_id, research_depth)


def _default_growth_pipeline_invoker(idea: str, venture_id: str, research_depth: str) -> dict[str, Any]:
    return run_growth_pipeline(idea, venture_id, research_depth)


_DEFAULT_COMPONENT_INVOKERS: dict[str, ComponentInvoker] = {
    "founder_orchestrator": _default_founder_orchestrator_invoker,
    "research_pipeline": _default_research_pipeline_invoker,
    "decision_engine": _default_decision_engine_invoker,
    "mvp_planner": _default_mvp_planner_invoker,
    "ai_builder": _default_ai_builder_invoker,
    "deployment_pipeline": _default_deployment_pipeline_invoker,
    "growth_pipeline": _default_growth_pipeline_invoker,
}

_component_invokers: dict[str, ComponentInvoker] = dict(_DEFAULT_COMPONENT_INVOKERS)


def get_component_invokers() -> dict[str, ComponentInvoker]:
    return _component_invokers


def set_component_invokers(overrides: dict[str, ComponentInvoker]) -> None:
    """Swappable, not cached - lets tests inject deterministic fake report
    dispatchers for any subset of the 7 reused Phase 4 components. Unspecified
    components keep their default (real) dispatcher.
    """
    global _component_invokers
    _component_invokers = {**_DEFAULT_COMPONENT_INVOKERS, **overrides}


def reset_component_invokers() -> None:
    global _component_invokers
    _component_invokers = dict(_DEFAULT_COMPONENT_INVOKERS)


GitStatusInvoker = Callable[[str], dict[str, Any]]


def _default_git_status_invoker(venture_id: str) -> dict[str, Any]:
    return run_git_operation("status", venture_id=venture_id)


_git_status_invoker: GitStatusInvoker = _default_git_status_invoker


def get_git_status_invoker() -> GitStatusInvoker:
    return _git_status_invoker


def set_git_status_invoker(fn: GitStatusInvoker) -> None:
    global _git_status_invoker
    _git_status_invoker = fn


def reset_git_status_invoker() -> None:
    global _git_status_invoker
    _git_status_invoker = _default_git_status_invoker


class FounderDashboardState(VentureState, total=False):
    """Extends VentureState - per its own docstring ("Extend this, never create a
    parallel state shape") - with the fields this workflow needs.
    """

    research_depth: str
    status: str
    error: str
    founder_orchestrator_result: dict[str, Any]
    research_result: dict[str, Any]
    research_data_quality: str
    decision_result: dict[str, Any]
    mvp_result: dict[str, Any]
    build_result: dict[str, Any]
    deployment_result: dict[str, Any]
    growth_result: dict[str, Any]
    git_result: dict[str, Any]
    dashboard: dict[str, Any]


# --------------------------------------------------------------------------- #
# Research re-invocation caching: decision/mvp/build/deployment/growth each
# independently re-derive research from scratch via a chain of single-
# upstream-dependency calls that all bottom out at decision_engine.py's own
# research_node -> get_research_pipeline_invoker() (traced call graph: 7
# independent full research passes per idea in the no-retry case). Every one
# of those 5 stages transitively funnels through that ONE seam, so installing
# a single override there - the same public DI seam decision_engine.py's own
# test suite already uses for fakes, just fed a real cached result instead -
# collapses all 5 redundant re-derivations into reuse of the one real result
# this module's own "research" node already computed, without touching any
# of the 5 components' own files or public contracts (they still receive an
# identically-shaped FounderResearchReport dict; only its origin changes).
# founder_orchestrator_node's own research call is NOT this cache's source -
# it invokes a different subsystem (the Phase 1 Research Supervisor subgraph
# via founder_pipeline.py), not run_research_pipeline, so its output isn't
# contract-compatible with what decision_engine.py's research_node expects.
# --------------------------------------------------------------------------- #

_research_cache_lock = threading.Lock()


def _install_cached_research_invoker(research_result: dict[str, Any]) -> None:
    with _research_cache_lock:
        set_research_pipeline_invoker(lambda idea, venture_id, research_depth: research_result)


def _reset_cached_research_invoker() -> None:
    with _research_cache_lock:
        reset_research_pipeline_invoker()


def _classify_research_data_quality(research_result: dict[str, Any]) -> str:
    """Bug #13 minimal fix: a visibility flag distinguishing genuine research
    from silent fallback-to-neutral-defaults under provider failure/rate-
    limit pressure. Classifies data already present in the research result
    (the same stage_results ok/failed breakdown decision_engine.py's own
    _extract_metrics() already derives stage_success_ratio from) - no new
    subsystem, just labeling what's already there.
    """
    stage_results = research_result.get("stage_results") or []
    if not stage_results:
        return "full_fallback"
    ok_count = sum(1 for s in stage_results if s.get("ok"))
    if ok_count == len(stage_results):
        return "genuine"
    if ok_count == 0:
        return "full_fallback"
    return "partial_fallback"


def _ok(entry: dict[str, Any]) -> bool:
    """A component result counts as "ok" for health purposes when it reports a
    genuinely successful/complete outcome - the specific status vocabulary
    differs slightly per component (each was designed independently across
    Components 1-7), so this checks the union of every "things went well"
    value actually used across them.
    """
    return str(entry.get("status", "")) in ("completed", "partial", "approved", "researched", "planned", "built", "validated")


def _call_component(key: str, idea: str, venture_id: str, research_depth: str) -> dict[str, Any]:
    fn = get_component_invokers()[key]
    try:
        return fn(idea, venture_id, research_depth)
    except GraphBubbleUp:
        raise
    except Exception as exc:
        return {"agent": key, "event": f"{key}_failed", "status": "failed", "error": str(exc)}


def _validate_inputs(state: FounderDashboardState) -> str | None:
    """research_depth is not validated here: run_founder_dashboard() (agents/
    founder/dashboard/agent.py) already coerces any value outside {"standard",
    "deep"} to "standard" before this graph ever runs - the same
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


def intake_node(state: FounderDashboardState) -> dict:
    bus = get_event_bus()
    venture_id = state.get("venture_id", "")
    idea = state.get("idea", "")

    bus.publish(AFOSEvent(type="dashboard_started", source_agent="founder_dashboard", venture_id=venture_id, payload={"idea": idea}))

    error = _validate_inputs(state)
    if error:
        return {"status": "rejected", "error": error}
    return {"status": "validated"}


def _route_after_intake(state: FounderDashboardState) -> str:
    return "founder_orchestrator" if state.get("status") == "validated" else "aggregate"


def founder_orchestrator_node(state: FounderDashboardState) -> dict:
    result = _call_component("founder_orchestrator", state.get("idea", ""), state.get("venture_id", ""), state.get("research_depth") or "standard")
    return {"founder_orchestrator_result": result}


def research_node(state: FounderDashboardState) -> dict:
    result = _call_component("research_pipeline", state.get("idea", ""), state.get("venture_id", ""), state.get("research_depth") or "standard")
    # Cache this one real result behind decision_engine.py's own DI seam so
    # decision/mvp/build/deployment/growth (all 5 transitively re-derive
    # research through that same seam) reuse it instead of each
    # independently re-computing it from scratch. Reset in aggregate_node,
    # which every graph path unconditionally reaches once this node has run.
    _install_cached_research_invoker(result)
    return {"research_result": result, "research_data_quality": _classify_research_data_quality(result)}


def decision_node(state: FounderDashboardState) -> dict:
    result = _call_component("decision_engine", state.get("idea", ""), state.get("venture_id", ""), state.get("research_depth") or "standard")
    return {"decision_result": result}


def mvp_node(state: FounderDashboardState) -> dict:
    result = _call_component("mvp_planner", state.get("idea", ""), state.get("venture_id", ""), state.get("research_depth") or "standard")
    return {"mvp_result": result}


def build_node(state: FounderDashboardState) -> dict:
    result = _call_component("ai_builder", state.get("idea", ""), state.get("venture_id", ""), state.get("research_depth") or "standard")
    return {"build_result": result}


def deployment_node(state: FounderDashboardState) -> dict:
    result = _call_component("deployment_pipeline", state.get("idea", ""), state.get("venture_id", ""), state.get("research_depth") or "standard")
    return {"deployment_result": result}


def growth_node(state: FounderDashboardState) -> dict:
    result = _call_component("growth_pipeline", state.get("idea", ""), state.get("venture_id", ""), state.get("research_depth") or "standard")
    return {"growth_result": result}


def git_status_node(state: FounderDashboardState) -> dict:
    venture_id = state.get("venture_id", "")
    try:
        result = get_git_status_invoker()(venture_id)
    except GraphBubbleUp:
        raise
    except Exception as exc:
        result = {"agent": "git_agent", "event": "git_operation_failed", "error": str(exc)}
    return {"git_result": result}


def _summarize_alerts_risks_actions(
    founder: dict[str, Any], research: dict[str, Any], decision: dict[str, Any], mvp: dict[str, Any],
    build: dict[str, Any], deployment: dict[str, Any], growth: dict[str, Any], git: dict[str, Any],
) -> tuple[list[str], list[str], list[str]]:
    """Pure Python, deterministic aggregation - no AI/LLM call. Alerts flag
    components that did not succeed; risks pull directly from the components
    that already computed their own (Decision Engine's warnings, MVP Planner's
    risks_and_dependencies); next actions surface Decision Engine's own
    next_actions plus a suggestion for whichever stage is the current
    bottleneck.
    """
    alerts: list[str] = []
    for label, result in (
        ("Founder Orchestrator", founder), ("Research Pipeline", research), ("Decision Engine", decision),
        ("MVP Planner", mvp), ("AI Builder", build), ("Deployment Pipeline", deployment), ("Growth Pipeline", growth),
    ):
        if not _ok(result):
            alerts.append(f"{label}: status='{result.get('status', 'unknown')}'" + (f" - {result.get('error')}" if result.get("error") else ""))
    if not str(git.get("event", "")).endswith("_completed"):
        alerts.append(f"Git Status: unavailable - {git.get('error', 'unknown error')}")

    risks: list[str] = list(decision.get("warnings") or [])
    for item in mvp.get("risks_and_dependencies") or []:
        risk_text = item.get("risk") if isinstance(item, dict) else str(item)
        if risk_text:
            risks.append(risk_text)
    if deployment.get("error"):
        risks.append(f"Deployment: {deployment['error']}")
    if growth.get("error"):
        risks.append(f"Growth: {growth['error']}")

    next_actions: list[str] = list(decision.get("next_actions") or [])
    if not _ok(research):
        next_actions.append("Re-run the Research Pipeline - no usable research data is currently available")
    elif not _ok(decision) or decision.get("recommendation") in ("PIVOT", "DROP"):
        next_actions.append("Revisit the idea based on the Decision Engine's recommendation before proceeding further")
    elif not _ok(mvp):
        next_actions.append("Generate a buildable MVP plan before attempting a build")
    elif not _ok(build):
        next_actions.append("Resolve the AI Builder's build failures before deploying")
    elif not _ok(deployment):
        next_actions.append("Bring the required infrastructure (Docker/PostgreSQL/Chroma/Ollama/n8n) online before deploying")
    elif not _ok(growth):
        next_actions.append("Bring the required growth backends (Ollama/Ads/CRM) online before launching growth activities")

    return alerts, risks, next_actions


def aggregate_node(state: FounderDashboardState) -> dict:
    # Always reset, whether research_node installed the override or not (a
    # rejected-input run never reaches this point having installed anything,
    # making this a harmless no-op in that case) - ensures no cached research
    # leaks into a later, unrelated dashboard run in this same process.
    _reset_cached_research_invoker()

    bus = get_event_bus()
    venture_id = state.get("venture_id", "")
    idea = state.get("idea", "")
    status = state.get("status", "failed")

    if status in ("rejected",):
        reason = state.get("error", "invalid input")
        dashboard = {
            "idea": idea, "venture_id": venture_id, "overall_health": "unknown", "status": "rejected",
            "venture_summary": {}, "research_summary": {}, "market_analysis": {}, "competitor_summary": {},
            "decision_scores": {}, "mvp_plan": {}, "build_status": {}, "deployment_status": {}, "growth_status": {},
            "git_status": {}, "research_data_quality": "unknown",
            "alerts": [reason], "risks": [], "recommended_next_actions": [], "summary": reason, "error": reason,
        }
        bus.publish(AFOSEvent(type="dashboard_failed", source_agent="founder_dashboard", venture_id=venture_id, payload={"reason": reason, "status": "rejected"}))
        return {"dashboard": dashboard, "history": [{"agent": "founder_dashboard", "event": "dashboard_failed", "status": "rejected"}]}

    founder = state.get("founder_orchestrator_result", {})
    research = state.get("research_result", {})
    decision = state.get("decision_result", {})
    mvp = state.get("mvp_result", {})
    build = state.get("build_result", {})
    deployment = state.get("deployment_result", {})
    growth = state.get("growth_result", {})
    git = state.get("git_result", {})

    component_results = [founder, research, decision, mvp, build, deployment, growth]
    ok_count = sum(1 for r in component_results if _ok(r))
    total = len(component_results)

    if ok_count == total:
        overall_health = "healthy"
    elif ok_count >= total // 2:
        overall_health = "degraded"
    elif ok_count > 0:
        overall_health = "critical"
    else:
        overall_health = "critical"

    alerts, risks, next_actions = _summarize_alerts_risks_actions(founder, research, decision, mvp, build, deployment, growth, git)

    dashboard = {
        "idea": idea,
        "venture_id": venture_id,
        "status": "completed",
        "overall_health": overall_health,
        "venture_summary": {
            "idea": idea,
            "venture_id": venture_id,
            "risk_level": founder.get("risk_level", "unknown"),
            "founder_status": founder.get("status", "unknown"),
            "required_agents": founder.get("required_agents", []),
        },
        "research_summary": {
            "status": research.get("status", "unknown"),
            "confidence_score": research.get("confidence_score", 0.0),
            "sources_count": len(research.get("sources") or []),
            "research_depth": research.get("research_depth", "standard"),
        },
        "market_analysis": research.get("market_analysis", {}),
        "competitor_summary": research.get("competitor_analysis", {}),
        "research_data_quality": state.get("research_data_quality", "unknown"),
        "decision_scores": {
            "opportunity_score": decision.get("opportunity_score", 0.0),
            "competition_score": decision.get("competition_score", 0.0),
            "risk_score": decision.get("risk_score", 0.0),
            "market_size_score": decision.get("market_size_score", 0.0),
            "build_difficulty": decision.get("build_difficulty", 0.0),
            "execution_complexity": decision.get("execution_complexity", 0.0),
            "revenue_potential": decision.get("revenue_potential", 0.0),
            "market_timing": decision.get("market_timing", 0.0),
            "ai_advantage": decision.get("ai_advantage", 0.0),
            "confidence_score": decision.get("confidence_score", 0.0),
            "overall_score": decision.get("overall_score", 0.0),
            "recommendation": decision.get("recommendation", "unknown"),
        },
        "mvp_plan": {
            "status": mvp.get("status", "unknown"),
            "feature_count": len(mvp.get("feature_list") or []),
            "tech_stack": mvp.get("tech_stack", []),
            "milestone_count": len(mvp.get("milestones") or []),
        },
        "build_status": {
            "status": build.get("status", "unknown"),
            "steps_completed": build.get("steps_completed", 0),
            "steps_total": build.get("steps_total", 0),
            "debug_retry_invocations": build.get("debug_retry_invocations", 0),
        },
        "deployment_status": {
            "status": deployment.get("status", "unknown"),
            "ready_stage_count": deployment.get("ready_stage_count", 0),
            "total_stage_count": deployment.get("total_stage_count", 0),
        },
        "growth_status": {
            "status": growth.get("status", "unknown"),
            "ready_stage_count": growth.get("ready_stage_count", 0),
            "total_stage_count": growth.get("total_stage_count", 0),
        },
        "git_status": {
            "available": str(git.get("event", "")).endswith("_completed"),
            "output": git.get("output", ""),
            "error": git.get("error", ""),
        },
        "alerts": alerts,
        "risks": risks,
        "recommended_next_actions": next_actions,
        "summary": f"Founder Dashboard for '{idea}': overall health is '{overall_health}' ({ok_count}/{total} components healthy).",
        "error": "",
    }

    bus.publish(AFOSEvent(type="dashboard_completed", source_agent="founder_dashboard", venture_id=venture_id, payload={"overall_health": overall_health, "ok_count": ok_count, "total": total}))
    return {"dashboard": dashboard, "history": [{"agent": "founder_dashboard", "event": "dashboard_completed", "overall_health": overall_health}]}


def build_founder_dashboard() -> StateGraph:
    """Intake -> Founder Orchestrator -> Research Pipeline -> Decision Engine ->
    MVP Planner -> AI Builder -> Deployment Pipeline -> Growth Pipeline -> Git
    Status -> Aggregate. Invalid input short-circuits straight to "aggregate"
    with a well-formed, explanatory dashboard - never attempting any component
    call. Every component call always runs regardless of a prior one's
    outcome - one component being unavailable/unsuccessful degrades only that
    section of the dashboard, never aborts the rest, matching every prior
    Phase 4 pipeline's "gracefully continue if one stage fails" precedent.
    """
    graph = StateGraph(FounderDashboardState)
    graph.add_node("intake", intake_node)
    graph.add_node("founder_orchestrator", founder_orchestrator_node)
    graph.add_node("research", research_node)
    graph.add_node("decision", decision_node)
    graph.add_node("mvp", mvp_node)
    graph.add_node("build", build_node)
    graph.add_node("deployment", deployment_node)
    graph.add_node("growth", growth_node)
    graph.add_node("git_status", git_status_node)
    graph.add_node("aggregate", aggregate_node)

    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", _route_after_intake, ["founder_orchestrator", "aggregate"])
    graph.add_edge("founder_orchestrator", "research")
    graph.add_edge("research", "decision")
    graph.add_edge("decision", "mvp")
    graph.add_edge("mvp", "build")
    graph.add_edge("build", "deployment")
    graph.add_edge("deployment", "growth")
    graph.add_edge("growth", "git_status")
    graph.add_edge("git_status", "aggregate")
    graph.add_edge("aggregate", END)
    return graph


def compile_founder_dashboard() -> CompiledStateGraph:
    # Deliberately compiled without a checkpointer, same precedent as every
    # prior Phase 4 pipeline component - this workflow runs straight through
    # with no approval gate of its own (the reused components own their own
    # approval gates internally, unchanged, and Founder Orchestrator is called
    # here with no budget, so it always auto-approves and never pauses).
    return build_founder_dashboard().compile()
