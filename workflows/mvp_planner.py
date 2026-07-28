from collections.abc import Callable
from typing import Any

from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents.founder.decision_engine.agent import run_decision_engine
from core.event_bus import AFOSEvent, get_event_bus
from core.state import VentureState

# --------------------------------------------------------------------------- #
# Dependency injection: the Founder Decision Engine is the *only* thing this
# module is allowed to call to get data - never a Tool Registry tool, an LLM, or
# a raw HTTP call. Swappable, not cached, so tests can inject a deterministic
# fake FounderDecisionReport-shaped dict instead of exercising the real research
# + decision chain. Same convention as every prior Phase 4 component's
# get_x_invoker()/set_x_invoker() pair.
# --------------------------------------------------------------------------- #

DecisionEngineInvoker = Callable[[str, str, str], dict[str, Any]]


def _default_decision_engine_invoker(idea: str, venture_id: str, research_depth: str) -> dict[str, Any]:
    return run_decision_engine(idea, venture_id, research_depth)


_decision_engine_invoker: DecisionEngineInvoker = _default_decision_engine_invoker


def get_decision_engine_invoker() -> DecisionEngineInvoker:
    return _decision_engine_invoker


def set_decision_engine_invoker(fn: DecisionEngineInvoker) -> None:
    global _decision_engine_invoker
    _decision_engine_invoker = fn


def reset_decision_engine_invoker() -> None:
    global _decision_engine_invoker
    _decision_engine_invoker = _default_decision_engine_invoker


class MVPPlannerState(VentureState, total=False):
    """Extends VentureState - per its own docstring ("Extend this, never create a
    parallel state shape") - with the fields this workflow needs.
    """

    research_depth: str
    status: str
    error: str
    decision_report: dict[str, Any]
    plan: dict[str, Any]


# --------------------------------------------------------------------------- #
# Pure-Python, deterministic MVP plan generation. No AI/LLM calls, no external
# API calls anywhere below this line - every section is a rule-based mapping
# from the Decision Engine's already-computed scores/recommendation onto a
# fixed set of product-planning templates. The same FounderDecisionReport
# always produces the identical MVPPlan.
# --------------------------------------------------------------------------- #

_PLANNABLE_RECOMMENDATIONS = {"BUILD NOW", "VALIDATE FIRST"}

_AI_KEYWORDS = (
    "ai", "artificial intelligence", "machine learning", " ml ", "llm", "gpt",
    "neural", "automat", "predict", "intelligent", "agent", "generative", "nlp",
    "computer vision", "chatbot", "copilot", "co-pilot",
)


def _has_ai_signal(idea: str, ai_advantage: float) -> bool:
    if ai_advantage >= 6.0:
        return True
    lowered = f" {idea.lower()} "
    return any(kw in lowered for kw in _AI_KEYWORDS)


def _build_prd(idea: str, decision: dict[str, Any], lean: bool) -> dict[str, Any]:
    reasons = decision.get("reasons") or []
    problem_statement = (
        f"Founders and teams currently lack an efficient way to achieve what '{idea}' proposes - "
        f"validated by a Decision Engine overall_score of {decision.get('overall_score', 0.0)}/10."
    )
    goals = [f"Deliver on: {reason}" for reason in reasons] or [
        "Establish a working product that proves the core value proposition"
    ]
    success_metrics = (
        ["Landing page signups", "Qualitative interview feedback", "Waitlist conversion rate"]
        if lean
        else ["Weekly active users", "Core workflow completion rate", "Revenue or paid-conversion signal"]
    )
    return {
        "title": f"{idea} - Product Requirements Document",
        "problem_statement": problem_statement,
        "target_users": ["Early adopters within the idea's identified target market"],
        "goals": goals,
        "success_metrics": success_metrics,
        "scope_summary": (
            "A minimal, validation-focused release to test demand before investing in a full build."
            if lean
            else "A functional MVP covering the core workflow end to end, ready for real early users."
        ),
    }


def _build_mvp_scope(lean: bool, ai_signal: bool, monetize: bool) -> dict[str, Any]:
    included = ["Core workflow (the single primary task the idea exists to solve)", "User authentication"]
    excluded = ["Advanced analytics", "Multi-language support", "Mobile native apps"]

    if lean:
        included += ["Landing page with signup capture", "Manual/lightweight feedback collection"]
        excluded += ["Billing/subscriptions", "AI-powered automation (deferred until validated)"]
        rationale = "Scope is deliberately minimal - the goal is validating demand, not building a full product."
    else:
        included += ["Basic dashboard for the user's data/progress"]
        if monetize:
            included.append("Billing & subscription management")
        else:
            excluded.append("Billing/subscriptions")
        if ai_signal:
            included.append("AI-powered core automation/assistant")
        else:
            excluded.append("AI-powered automation")
        rationale = "Scope covers the full core workflow end to end so early users get real value."

    return {"included": included, "excluded": excluded, "rationale": rationale}


def _build_feature_list(lean: bool, ai_signal: bool, monetize: bool, big_market: bool) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = [
        {"name": "User Authentication", "description": "Sign up, log in, and manage a basic account", "priority": "must_have"},
        {"name": "Core Workflow", "description": "The single primary workflow that delivers the idea's core value", "priority": "must_have"},
    ]

    if lean:
        features.append({"name": "Landing Page & Waitlist", "description": "Capture demand signal before full build", "priority": "must_have"})
        features.append({"name": "Feedback Capture", "description": "Lightweight in-product or email feedback collection", "priority": "must_have"})
        features.append({"name": "Billing & Subscriptions", "description": "Payment collection and plan management", "priority": "nice_to_have"})
        features.append({"name": "AI-Powered Assistant", "description": "AI-driven automation of the core workflow", "priority": "nice_to_have"})
    else:
        features.append({"name": "Basic Dashboard", "description": "A simple view of the user's data/progress", "priority": "must_have"})
        features.append(
            {"name": "Billing & Subscriptions", "description": "Payment collection and plan management", "priority": "must_have" if monetize else "should_have"}
        )
        features.append(
            {"name": "AI-Powered Assistant", "description": "AI-driven automation of the core workflow", "priority": "must_have" if ai_signal else "should_have"}
        )
        if big_market:
            features.append({"name": "Usage Analytics Dashboard", "description": "Track growth and engagement early, given the large addressable market", "priority": "should_have"})

    return features


def _build_tech_stack(ai_signal: bool) -> list[dict[str, str]]:
    stack = [
        {"layer": "frontend", "technology": "React + TypeScript (Next.js)", "rationale": "Fast to build, large ecosystem, easy hiring"},
        {"layer": "backend", "technology": "Python + FastAPI", "rationale": "Rapid API development with strong typing and async support"},
        {"layer": "database", "technology": "PostgreSQL", "rationale": "Reliable relational store, scales well beyond MVP"},
        {"layer": "auth", "technology": "OAuth2 / JWT-based session auth", "rationale": "Standard, well-supported authentication approach"},
        {"layer": "deployment", "technology": "Docker + a managed container host (e.g. Fly.io/Render/ECS)", "rationale": "Simple, portable deployment without heavy DevOps investment"},
    ]
    if ai_signal:
        stack.append({"layer": "ai", "technology": "LangGraph orchestration + a hosted LLM API (with a local model fallback)", "rationale": "Idea has a clear AI-native advantage - needs an orchestration layer from day one"})
    return stack


def _build_database_outline(ai_signal: bool, monetize: bool) -> list[dict[str, Any]]:
    tables = [
        {"name": "users", "columns": ["id", "email", "password_hash", "created_at"], "purpose": "Authentication and account ownership"},
        {"name": "core_entities", "columns": ["id", "user_id", "data", "status", "created_at", "updated_at"], "purpose": "The primary domain object the core workflow operates on"},
        {"name": "events", "columns": ["id", "user_id", "type", "payload", "created_at"], "purpose": "Audit log / activity feed for the core workflow"},
    ]
    if monetize:
        tables.append({"name": "subscriptions", "columns": ["id", "user_id", "plan", "status", "renewed_at"], "purpose": "Billing and plan management"})
    if ai_signal:
        tables.append({"name": "embeddings", "columns": ["id", "core_entity_id", "vector", "metadata"], "purpose": "Semantic search/recall for the AI-powered assistant"})
    return tables


def _build_api_outline(feature_list: list[dict[str, Any]]) -> list[dict[str, str]]:
    endpoints = [
        {"method": "POST", "path": "/api/auth/signup", "description": "Create a new user account"},
        {"method": "POST", "path": "/api/auth/login", "description": "Authenticate and obtain a session token"},
    ]
    feature_names = {f["name"] for f in feature_list}
    if "Core Workflow" in feature_names:
        endpoints += [
            {"method": "GET", "path": "/api/core", "description": "List the user's core workflow items"},
            {"method": "POST", "path": "/api/core", "description": "Create a new core workflow item"},
            {"method": "GET", "path": "/api/core/{id}", "description": "Get a single core workflow item"},
        ]
    if "Basic Dashboard" in feature_names:
        endpoints.append({"method": "GET", "path": "/api/dashboard", "description": "Get the user's dashboard summary"})
    if "Billing & Subscriptions" in feature_names:
        endpoints += [
            {"method": "POST", "path": "/api/billing/checkout", "description": "Start a subscription checkout session"},
            {"method": "GET", "path": "/api/billing/subscription", "description": "Get the user's current subscription status"},
        ]
    if "AI-Powered Assistant" in feature_names:
        endpoints.append({"method": "POST", "path": "/api/assistant/query", "description": "Invoke the AI-powered assistant on the user's data"})
    if "Landing Page & Waitlist" in feature_names:
        endpoints.append({"method": "POST", "path": "/api/waitlist", "description": "Capture a waitlist signup"})
    if "Feedback Capture" in feature_names:
        endpoints.append({"method": "POST", "path": "/api/feedback", "description": "Submit user feedback"})
    return endpoints


def _build_folder_structure(venture_id: str, ai_signal: bool, monetize: bool) -> list[str]:
    """Every path is scoped under generated_ventures/{venture_id}/ rather than
    the repo root - these generic names (backend/app/main.py, docs/PRD.md,
    ...) collide with the AFOS platform's own real source tree at the repo
    root otherwise, silently no-opping every create_file call (see
    docs/bug_investigation_log.md's Fix A). venture_id is guaranteed
    non-empty here - _validate_inputs() rejects a missing one before the
    graph ever reaches plan_generation_node.
    """
    prefix = f"generated_ventures/{venture_id}/"
    structure = [
        prefix + "backend/",
        prefix + "backend/app/",
        prefix + "backend/app/main.py",
        prefix + "backend/app/api/",
        prefix + "backend/app/api/auth.py",
        prefix + "backend/app/api/core.py",
        prefix + "backend/app/models/",
        prefix + "backend/app/db/",
        prefix + "backend/tests/",
        prefix + "frontend/",
        prefix + "frontend/src/",
        prefix + "frontend/src/pages/",
        prefix + "frontend/src/components/",
        prefix + "database/",
        prefix + "database/migrations/",
        prefix + "docker/",
        prefix + "docs/",
        prefix + "docs/PRD.md",
    ]
    if ai_signal:
        structure.append(prefix + "backend/app/ai/")
    if monetize:
        structure.append(prefix + "backend/app/api/billing.py")
    return structure


def _build_timeline(recommendation: str, build_difficulty: float, execution_complexity: float) -> list[dict[str, Any]]:
    base_weeks = 10 if recommendation == "BUILD NOW" else 4
    difficulty_factor = (10.0 - (build_difficulty + execution_complexity) / 2.0) / 10.0
    total_weeks = max(2, round(base_weeks * (1.0 + max(0.0, difficulty_factor))))

    setup_weeks = max(1, round(total_weeks * 0.15))
    core_weeks = max(1, round(total_weeks * 0.55))
    testing_weeks = max(1, total_weeks - setup_weeks - core_weeks - max(1, round(total_weeks * 0.1)))
    launch_weeks = max(1, total_weeks - setup_weeks - core_weeks - testing_weeks)

    cursor = 0
    phases = []
    for phase_name, duration, tasks in (
        ("Setup & Architecture", setup_weeks, ["Repository/project scaffolding", "Tech stack setup", "Database schema creation"]),
        ("Core Development", core_weeks, ["Build the core workflow end to end", "Implement authentication", "Wire up the chosen tech stack"]),
        ("Testing & Hardening", testing_weeks, ["Write automated tests", "Manual QA pass", "Fix critical bugs"]),
        ("Launch Prep", launch_weeks, ["Deploy to production", "Prepare onboarding/marketing materials", "Launch to early users"]),
    ):
        start, end = cursor + 1, cursor + duration
        phases.append({"phase": phase_name, "weeks": f"Week {start}-{end}", "tasks": tasks})
        cursor = end

    return phases


def _build_milestones(timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    milestones = []
    for phase in timeline:
        end_week = int(phase["weeks"].split("-")[-1])
        milestones.append({"name": f"{phase['phase']} complete", "target_week": end_week, "deliverable": phase["tasks"][-1]})
    return milestones


def _build_risks_and_dependencies(decision: dict[str, Any], build_difficulty: float, execution_complexity: float) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    for warning in decision.get("warnings") or []:
        risks.append({"risk": warning, "severity": "medium", "mitigation": "Revisit during MVP validation before committing further resources"})
    if build_difficulty < 4.0:
        risks.append({"risk": "High build difficulty may extend the timeline beyond estimates", "severity": "high", "mitigation": "Break the core workflow into the smallest possible shippable slice first"})
    if execution_complexity < 4.0:
        risks.append({"risk": "High execution complexity increases the chance of scope creep", "severity": "high", "mitigation": "Freeze MVP scope before development begins; defer non-essential features"})
    if decision.get("confidence_score", 0.0) < 4.0:
        risks.append({"risk": "Low confidence in the underlying research/decision scores", "severity": "medium", "mitigation": "Run deeper research (research_depth='deep') before committing significant resources"})
    risks.append({"risk": "Team/resource availability", "severity": "low", "mitigation": "Confirm team capacity against the proposed timeline before kickoff"})
    return risks


def _generate_plan(idea: str, decision: dict[str, Any], venture_id: str) -> dict[str, Any]:
    recommendation = decision.get("recommendation", "DROP")
    lean = recommendation == "VALIDATE FIRST"
    ai_signal = _has_ai_signal(idea, float(decision.get("ai_advantage", 0.0)))
    monetize = float(decision.get("revenue_potential", 0.0)) >= 6.0
    big_market = float(decision.get("market_size_score", 0.0)) >= 7.0
    build_difficulty = float(decision.get("build_difficulty", 5.0))
    execution_complexity = float(decision.get("execution_complexity", 5.0))

    feature_list = _build_feature_list(lean, ai_signal, monetize, big_market)
    timeline = _build_timeline(recommendation, build_difficulty, execution_complexity)

    return {
        "idea": idea,
        "prd": _build_prd(idea, decision, lean),
        "mvp_scope": _build_mvp_scope(lean, ai_signal, monetize),
        "feature_list": feature_list,
        "tech_stack": _build_tech_stack(ai_signal),
        "database_outline": _build_database_outline(ai_signal, monetize),
        "api_outline": _build_api_outline(feature_list),
        "folder_structure": _build_folder_structure(venture_id, ai_signal, monetize),
        "development_timeline": timeline,
        "milestones": _build_milestones(timeline),
        "risks_and_dependencies": _build_risks_and_dependencies(decision, build_difficulty, execution_complexity),
        "status": "completed",
    }


def _skipped_plan(idea: str, status: str, reason: str) -> dict[str, Any]:
    return {
        "idea": idea,
        "prd": {"title": f"{idea} - Product Requirements Document", "problem_statement": "", "target_users": [], "goals": [], "success_metrics": [], "scope_summary": ""},
        "mvp_scope": {"included": [], "excluded": [], "rationale": reason},
        "feature_list": [],
        "tech_stack": [],
        "database_outline": [],
        "api_outline": [],
        "folder_structure": [],
        "development_timeline": [],
        "milestones": [],
        "risks_and_dependencies": [{"risk": reason, "severity": "high", "mitigation": "Address the underlying issue before planning an MVP"}],
        "status": status,
    }


def _validate_inputs(state: MVPPlannerState) -> str | None:
    """research_depth is not validated here: run_mvp_planner() (agents/founder/
    mvp_planner/agent.py) already coerces any value outside {"standard", "deep"}
    to "standard" before this graph ever runs - the same silent-coercion
    convention used by Components 2 and 3 for the same field.
    """
    idea = (state.get("idea") or "").strip()
    if not idea:
        return "idea must not be empty"
    venture_id = (state.get("venture_id") or "").strip()
    if not venture_id:
        return "venture_id is required"
    return None


def intake_node(state: MVPPlannerState) -> dict:
    bus = get_event_bus()
    venture_id = state.get("venture_id", "")
    idea = state.get("idea", "")

    bus.publish(AFOSEvent(type="mvp_planner_started", source_agent="mvp_planner", venture_id=venture_id, payload={"idea": idea}))

    error = _validate_inputs(state)
    if error:
        return {"status": "rejected", "error": error}
    return {"status": "validated"}


def _route_after_intake(state: MVPPlannerState) -> str:
    return "decision" if state.get("status") == "validated" else "aggregate"


def decision_node(state: MVPPlannerState) -> dict:
    """The only place this workflow ever reaches outside itself - and even here,
    only through the already-completed Founder Decision Engine's own public
    function, never a Tool Registry tool, LLM, or raw external call directly.
    """
    try:
        result = get_decision_engine_invoker()(
            state.get("idea", ""), state.get("venture_id", ""), state.get("research_depth") or "standard",
        )
        return {"decision_report": result, "status": "decided"}
    except GraphBubbleUp:
        raise
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


def _route_after_decision(state: MVPPlannerState) -> str:
    if state.get("status") != "decided":
        return "aggregate"
    decision = state.get("decision_report", {})
    if decision.get("status") != "completed":
        return "aggregate"
    if decision.get("recommendation") not in _PLANNABLE_RECOMMENDATIONS:
        return "aggregate"
    return "plan_generation"


def plan_generation_node(state: MVPPlannerState) -> dict:
    plan = _generate_plan(state.get("idea", ""), state.get("decision_report", {}), state.get("venture_id", ""))
    return {"plan": plan, "status": "planned"}


def aggregate_node(state: MVPPlannerState) -> dict:
    bus = get_event_bus()
    venture_id = state.get("venture_id", "")
    idea = state.get("idea", "")
    status = state.get("status", "failed")

    if status == "planned":
        plan = state.get("plan", {})
        bus.publish(
            AFOSEvent(type="mvp_planner_completed", source_agent="mvp_planner", venture_id=venture_id, payload={"feature_count": len(plan.get("feature_list", []))})
        )
        return {"plan": plan, "history": [{"agent": "mvp_planner", "event": "mvp_planner_completed", "status": "completed"}]}

    if status == "rejected":
        reason = state.get("error", "invalid input")
        plan = _skipped_plan(idea, "rejected", reason)
    elif status == "failed":
        reason = state.get("error", "unexpected failure")
        plan = _skipped_plan(idea, "failed", reason)
    else:
        decision = state.get("decision_report", {})
        recommendation = decision.get("recommendation", "unknown")
        decision_status = decision.get("status", "unknown")
        if decision_status != "completed":
            reason = f"Decision Engine did not complete (status='{decision_status}') - MVP planning skipped"
        else:
            reason = f"Decision Engine recommends '{recommendation}' - revisit the idea before planning an MVP"
        plan = _skipped_plan(idea, "not_recommended", reason)

    bus.publish(
        AFOSEvent(type="mvp_planner_failed", source_agent="mvp_planner", venture_id=venture_id, payload={"reason": plan["mvp_scope"]["rationale"], "status": plan["status"]})
    )
    return {"plan": plan, "history": [{"agent": "mvp_planner", "event": "mvp_planner_failed", "status": plan["status"]}]}


def build_mvp_planner() -> StateGraph:
    """Founder Decision Engine -> (route on recommendation) -> Plan Generation ->
    Aggregate. Invalid input, a failed decision call, a non-completed decision, or
    a PIVOT/DROP recommendation all short-circuit to "aggregate" with a
    well-formed, empty MVPPlan explaining why planning was skipped - never a
    partially-populated one.
    """
    graph = StateGraph(MVPPlannerState)
    graph.add_node("intake", intake_node)
    graph.add_node("decision", decision_node)
    graph.add_node("plan_generation", plan_generation_node)
    graph.add_node("aggregate", aggregate_node)

    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", _route_after_intake, ["decision", "aggregate"])
    graph.add_conditional_edges("decision", _route_after_decision, ["plan_generation", "aggregate"])
    graph.add_edge("plan_generation", "aggregate")
    graph.add_edge("aggregate", END)
    return graph


def compile_mvp_planner() -> CompiledStateGraph:
    # Deliberately compiled without a checkpointer, same precedent as
    # workflows/decision_engine.py's compile_decision_engine() - this workflow
    # runs straight through with no approval gate.
    return build_mvp_planner().compile()
