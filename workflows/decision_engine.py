import re
from collections.abc import Callable
from typing import Any

from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents.founder.research_pipeline.agent import run_research_pipeline
from core.event_bus import AFOSEvent, get_event_bus
from core.state import VentureState

# --------------------------------------------------------------------------- #
# Dependency injection: the Research Pipeline is the *only* thing this module
# is allowed to call to get data - never a Tool Registry tool, an LLM, or a raw
# HTTP call. Swappable, not cached, so tests can inject a deterministic fake
# FounderResearchReport-shaped dict instead of exercising the real (network/
# LLM-dependent) research workers. Same convention as every prior component's
# get_x_provider()/set_x_provider() pair.
# --------------------------------------------------------------------------- #

ResearchPipelineInvoker = Callable[[str, str, str], dict[str, Any]]


def _default_research_pipeline_invoker(idea: str, venture_id: str, research_depth: str) -> dict[str, Any]:
    return run_research_pipeline(idea, venture_id, research_depth)


_research_pipeline_invoker: ResearchPipelineInvoker = _default_research_pipeline_invoker


def get_research_pipeline_invoker() -> ResearchPipelineInvoker:
    return _research_pipeline_invoker


def set_research_pipeline_invoker(fn: ResearchPipelineInvoker) -> None:
    global _research_pipeline_invoker
    _research_pipeline_invoker = fn


def reset_research_pipeline_invoker() -> None:
    global _research_pipeline_invoker
    _research_pipeline_invoker = _default_research_pipeline_invoker


class DecisionEngineState(VentureState, total=False):
    """Extends VentureState - per its own docstring ("Extend this, never create a
    parallel state shape") - with the fields this workflow needs.
    """

    research_depth: str
    status: str
    error: str
    research_result: dict[str, Any]
    metrics: dict[str, Any]
    scores: dict[str, float]
    report: dict[str, Any]


# --------------------------------------------------------------------------- #
# Pure-Python scoring. No AI/LLM calls anywhere below this line - every score is
# arithmetic or string/keyword parsing over data the (already-completed)
# Research Pipeline produced. Every score is normalized to a 0-10 scale where
# HIGHER IS ALWAYS MORE FAVORABLE to building the venture (including
# risk_score, build_difficulty, and execution_complexity - a high risk_score
# means LOW risk, a high build_difficulty means EASY to build, a high
# execution_complexity means SIMPLE execution), so overall_score's weighted
# average is directly meaningful: higher overall_score = stronger case to build.
# --------------------------------------------------------------------------- #

_MONEY_RE = re.compile(r"([\d.]+)\s*([KMBT])", re.IGNORECASE)
_PERCENT_RE = re.compile(r"([\d.]+)\s*%")
_DIFFICULTY_FAVORABILITY = {"low": 8.0, "medium": 5.0, "high": 2.0}
_AI_KEYWORDS = (
    "ai", "artificial intelligence", "machine learning", " ml ", "llm", "gpt",
    "neural", "automat", "predict", "intelligent", "agent", "generative", "nlp",
    "computer vision", "chatbot", "copilot", "co-pilot",
)
_MONEY_MULTIPLIERS = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}

_SCORE_WEIGHTS: dict[str, float] = {
    "opportunity_score": 0.18,
    "market_size_score": 0.14,
    "revenue_potential": 0.14,
    "competition_score": 0.12,
    "market_timing": 0.10,
    "risk_score": 0.12,
    "build_difficulty": 0.08,
    "execution_complexity": 0.07,
    "ai_advantage": 0.05,
}


def _clamp(value: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, value))


def _parse_money_to_score(text: str) -> float:
    """'$5B' -> a high market_size_score component; '$500M' -> a mid one; no
    parseable figure -> a neutral baseline. Pure regex parsing, no external call.
    """
    if not text:
        return 5.0
    match = _MONEY_RE.search(text)
    if not match:
        return 5.0
    value = float(match.group(1))
    usd = value * _MONEY_MULTIPLIERS[match.group(2).upper()]
    if usd >= 1e10:
        return 10.0
    if usd >= 1e9:
        return 8.5
    if usd >= 1e8:
        return 7.0
    if usd >= 1e7:
        return 5.0
    return 3.0


def _parse_percent_bonus(text: str) -> float:
    if not text:
        return 0.0
    match = _PERCENT_RE.search(text)
    if not match:
        return 0.0
    pct = float(match.group(1))
    if pct >= 20:
        return 1.5
    if pct >= 10:
        return 1.0
    if pct >= 5:
        return 0.5
    return 0.0


def _revenue_text_to_score(text: str) -> float:
    lowered = (text or "").lower()
    if any(k in lowered for k in ("very high", "excellent", "exceptional")):
        return 9.0
    if any(k in lowered for k in ("high", "strong", "significant")):
        return 7.5
    if any(k in lowered for k in ("medium", "moderate", "decent")):
        return 5.0
    if any(k in lowered for k in ("low", "limited", "weak")):
        return 2.5
    return 5.0


def _extract_metrics(research_result: dict[str, Any]) -> dict[str, Any]:
    """Pulls raw fields out of the (already-completed) Research Pipeline's
    FounderResearchReport dict into a flat metrics dict - pure data extraction,
    no scoring arithmetic yet.
    """
    market = research_result.get("market_analysis") or {}
    competitor = research_result.get("competitor_analysis") or {}
    trend = research_result.get("trend_analysis") or {}
    opportunity = research_result.get("opportunity_analysis") or {}
    validation = research_result.get("idea_validation") or {}
    stage_results = research_result.get("stage_results") or []

    return {
        "idea_text": research_result.get("idea", ""),
        "research_status": research_result.get("status", "no_data"),
        "sources_count": len(research_result.get("sources") or []),
        "stage_success_ratio": (sum(1 for s in stage_results if s.get("ok")) / len(stage_results)) if stage_results else 0.0,
        "market_size_tam": market.get("market_size_tam", ""),
        "cagr": market.get("cagr", ""),
        "growth_drivers_count": len(market.get("growth_drivers") or []),
        "market_risks_count": len(market.get("market_risks") or []),
        "market_confidence": market.get("confidence_score", 0.0),
        "direct_competitors_count": len(competitor.get("direct_competitors") or []),
        "market_gaps_count": len(competitor.get("market_gaps") or []),
        "differentiation_opportunities_count": len(competitor.get("differentiation_opportunities") or []),
        "competitor_confidence": competitor.get("confidence_score", 0.0),
        "emerging_trends_count": len(trend.get("emerging_trends") or []),
        "declining_trends_count": len(trend.get("declining_trends") or []),
        "trend_score": trend.get("trend_score", 0.0),
        "trend_confidence": trend.get("confidence_score", 0.0),
        "opportunity_priority_score": opportunity.get("priority_score", 0.0),
        "opportunity_difficulty": (opportunity.get("difficulty") or "").lower(),
        "monetization_model": opportunity.get("monetization_model", ""),
        "opportunity_confidence": opportunity.get("confidence_score", 0.0),
        "validation_completed": validation.get("event") == "idea_validation_completed",
        "validation_competition_score": validation.get("competition_score"),
        "validation_timing_score": validation.get("timing_score"),
        "validation_execution_score": validation.get("execution_score"),
        "validation_revenue_potential_text": validation.get("revenue_potential", ""),
        "validation_risks_count": len(validation.get("risks") or []),
        "validation_confidence": validation.get("confidence_score", 0.0),
    }


def _calculate_scores(metrics: dict[str, Any]) -> dict[str, float]:
    """Pure arithmetic over `metrics` - no randomness, no I/O, no AI. The same
    metrics dict always produces the same scores.
    """
    validated = bool(metrics.get("validation_completed"))

    if metrics["opportunity_priority_score"]:
        opportunity_score = _clamp(metrics["opportunity_priority_score"] * 10.0)
    else:
        opportunity_score = _clamp(
            5.0 + metrics["market_gaps_count"] * 0.7 + metrics["differentiation_opportunities_count"] * 0.7 + metrics["growth_drivers_count"] * 0.3
        )

    v_competition = metrics.get("validation_competition_score")
    if validated and v_competition is not None:
        competition_score = _clamp(float(v_competition))
    else:
        competition_score = _clamp(8.0 - metrics["direct_competitors_count"] * 1.2 + metrics["market_gaps_count"] * 0.5)

    total_risks = metrics["market_risks_count"] + metrics["validation_risks_count"]
    risk_score = _clamp(9.0 - total_risks * 1.0)

    market_size_score = _clamp(_parse_money_to_score(metrics["market_size_tam"]) + _parse_percent_bonus(metrics["cagr"]))

    base_difficulty = _DIFFICULTY_FAVORABILITY.get(metrics["opportunity_difficulty"], 5.0)
    v_execution = metrics.get("validation_execution_score")
    if validated and v_execution is not None:
        build_difficulty = _clamp((base_difficulty + float(v_execution)) / 2)
        execution_complexity = _clamp(float(v_execution))
    else:
        build_difficulty = _clamp(base_difficulty)
        execution_complexity = _clamp(base_difficulty - metrics["validation_risks_count"] * 0.3)

    revenue_text_score = _revenue_text_to_score(metrics["validation_revenue_potential_text"])
    monetization_bonus = 1.0 if metrics["monetization_model"] else 0.0
    market_bonus = (market_size_score - 5.0) * 0.2
    revenue_potential = _clamp(revenue_text_score + monetization_bonus + market_bonus)

    if metrics["trend_score"]:
        market_timing = _clamp(metrics["trend_score"] * 10.0)
    else:
        market_timing = _clamp(5.0 + (metrics["emerging_trends_count"] - metrics["declining_trends_count"]) * 0.8)
    v_timing = metrics.get("validation_timing_score")
    if validated and v_timing is not None:
        market_timing = _clamp((market_timing + float(v_timing)) / 2)

    keyword_hits = sum(1 for kw in _AI_KEYWORDS if kw in f" {metrics['idea_text'].lower()} ")
    ai_advantage = _clamp(5.0 + keyword_hits * 1.2)

    confidences = [
        c for c in (
            metrics["market_confidence"], metrics["competitor_confidence"], metrics["trend_confidence"],
            metrics["opportunity_confidence"], metrics["validation_confidence"],
        ) if c
    ]
    avg_confidence = (sum(confidences) / len(confidences)) if confidences else 0.0
    confidence_score = _clamp(avg_confidence * 10.0 * 0.6 + metrics["stage_success_ratio"] * 10.0 * 0.4)

    return {
        "opportunity_score": round(opportunity_score, 2),
        "competition_score": round(competition_score, 2),
        "risk_score": round(risk_score, 2),
        "market_size_score": round(market_size_score, 2),
        "build_difficulty": round(build_difficulty, 2),
        "execution_complexity": round(execution_complexity, 2),
        "revenue_potential": round(revenue_potential, 2),
        "market_timing": round(market_timing, 2),
        "ai_advantage": round(ai_advantage, 2),
        "confidence_score": round(confidence_score, 2),
    }


def _overall_score(scores: dict[str, float]) -> float:
    total = sum(scores[key] * weight for key, weight in _SCORE_WEIGHTS.items())
    return round(_clamp(total), 2)


def _recommendation(overall_score: float) -> str:
    if overall_score >= 8:
        return "BUILD NOW"
    if overall_score >= 6:
        return "VALIDATE FIRST"
    if overall_score >= 4:
        return "PIVOT"
    return "DROP"


def _build_reasons(scores: dict[str, float], metrics: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if scores["opportunity_score"] >= 7:
        reasons.append(f"Strong opportunity signal (opportunity_score={scores['opportunity_score']})")
    if scores["market_size_score"] >= 7:
        reasons.append(f"Large addressable market ({metrics['market_size_tam'] or 'estimated large based on available signals'})")
    if scores["competition_score"] >= 7:
        reasons.append("Low competitive saturation detected")
    if scores["revenue_potential"] >= 7:
        reasons.append("Strong revenue potential identified")
    if scores["market_timing"] >= 7:
        reasons.append("Favorable market timing (emerging trends outweigh declining ones)")
    if scores["ai_advantage"] >= 7:
        reasons.append("Idea has a clear AI-native advantage")
    if scores["risk_score"] >= 7:
        reasons.append("Few significant risks identified in the available research")
    if not reasons:
        reasons.append("No standout strengths identified from the available research data")
    return reasons


def _build_warnings(scores: dict[str, float], metrics: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if metrics["research_status"] == "no_data":
        warnings.append("Research Pipeline returned no usable data - these scores are conservative baseline estimates")
    if scores["competition_score"] < 4:
        warnings.append("High competitive saturation - differentiation is critical")
    if scores["risk_score"] < 4:
        warnings.append("Multiple significant risks identified in the research")
    if scores["build_difficulty"] < 4:
        warnings.append("High build difficulty - expect a longer or costlier build")
    if scores["market_size_score"] < 4:
        warnings.append("Market size signal is weak or unconfirmed")
    if scores["confidence_score"] < 4:
        warnings.append("Low confidence in these scores - insufficient research data was available")
    if not metrics["monetization_model"]:
        warnings.append("No clear monetization model identified")
    return warnings


def _next_actions(recommendation: str) -> list[str]:
    return {
        "BUILD NOW": [
            "Begin MVP development",
            "Assemble the initial build team/resources",
            "Define a launch timeline and success metrics",
        ],
        "VALIDATE FIRST": [
            "Conduct customer discovery interviews",
            "Build a landing page or prototype to test demand",
            "Re-run research with research_depth='deep' for stronger signal",
        ],
        "PIVOT": [
            "Revisit the core value proposition",
            "Explore adjacent market segments or use cases",
            "Re-validate with a narrower or different target audience",
        ],
        "DROP": [
            "Document learnings for future reference",
            "Explore a substantially different idea",
            "Do not allocate further resources to this venture",
        ],
    }[recommendation]


_BASELINE_SCORE_KEYS = (
    "opportunity_score", "competition_score", "risk_score", "market_size_score", "build_difficulty",
    "execution_complexity", "revenue_potential", "market_timing", "ai_advantage", "confidence_score",
)


def _terminal_report(idea: str, status: str, reason: str) -> dict[str, Any]:
    """Used for both the "rejected" (invalid input) and "failed" (unexpected
    exception) terminal states - a safe, well-formed, all-zero report rather
    than a partially-populated one.
    """
    return {
        "idea": idea,
        **{key: 0.0 for key in _BASELINE_SCORE_KEYS},
        "overall_score": 0.0,
        "recommendation": "DROP",
        "reasons": [],
        "warnings": [reason],
        "next_actions": _next_actions("DROP"),
        "status": status,
    }


def _validate_inputs(state: DecisionEngineState) -> str | None:
    """research_depth is not validated here: run_decision_engine() (agents/founder/
    decision_engine/agent.py) already coerces any value outside {"standard",
    "deep"} to "standard" before this graph ever runs - the same silent-coercion
    behavior Component 2's research_pipeline agent uses for the same field, so an
    invalid depth never reaches this state.
    """
    idea = (state.get("idea") or "").strip()
    if not idea:
        return "idea must not be empty"
    venture_id = (state.get("venture_id") or "").strip()
    if not venture_id:
        return "venture_id is required"
    return None


def intake_node(state: DecisionEngineState) -> dict:
    bus = get_event_bus()
    venture_id = state.get("venture_id", "")
    idea = state.get("idea", "")

    bus.publish(AFOSEvent(type="decision_started", source_agent="decision_engine", venture_id=venture_id, payload={"idea": idea}))

    error = _validate_inputs(state)
    if error:
        return {"status": "rejected", "error": error}
    return {"status": "validated"}


def _route_after_intake(state: DecisionEngineState) -> str:
    return "research" if state.get("status") == "validated" else "aggregate"


def research_node(state: DecisionEngineState) -> dict:
    """The only place this workflow ever reaches outside itself - and even here,
    only through the already-completed Research Pipeline's own public function,
    never a Tool Registry tool, LLM, or raw external call directly.
    """
    try:
        result = get_research_pipeline_invoker()(
            state.get("idea", ""), state.get("venture_id", ""), state.get("research_depth") or "standard",
        )
        return {"research_result": result, "status": "researched"}
    except GraphBubbleUp:
        raise
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


def _route_after_research(state: DecisionEngineState) -> str:
    return "extract_metrics" if state.get("status") == "researched" else "aggregate"


def extract_metrics_node(state: DecisionEngineState) -> dict:
    return {"metrics": _extract_metrics(state.get("research_result", {}))}


def calculate_scores_node(state: DecisionEngineState) -> dict:
    return {"scores": _calculate_scores(state.get("metrics", {}))}


def aggregate_node(state: DecisionEngineState) -> dict:
    bus = get_event_bus()
    venture_id = state.get("venture_id", "")
    idea = state.get("idea", "")
    status = state.get("status", "failed")

    if status in ("rejected", "failed"):
        reason = state.get("error", "unknown failure")
        report = _terminal_report(idea, status, reason)
        bus.publish(AFOSEvent(type="decision_failed", source_agent="decision_engine", venture_id=venture_id, payload={"reason": reason, "status": status}))
        return {"report": report, "history": [{"agent": "decision_engine", "event": "decision_failed", "status": status, "reason": reason}]}

    scores = state.get("scores", {})
    metrics = state.get("metrics", {})
    overall = _overall_score(scores)
    recommendation = _recommendation(overall)

    report = {
        "idea": idea,
        **scores,
        "overall_score": overall,
        "recommendation": recommendation,
        "reasons": _build_reasons(scores, metrics),
        "warnings": _build_warnings(scores, metrics),
        "next_actions": _next_actions(recommendation),
        "status": "completed",
    }

    bus.publish(
        AFOSEvent(
            type="decision_completed", source_agent="decision_engine", venture_id=venture_id,
            payload={"overall_score": overall, "recommendation": recommendation},
        )
    )
    return {
        "report": report,
        "history": [{"agent": "decision_engine", "event": "decision_completed", "overall_score": overall, "recommendation": recommendation}],
    }


def build_decision_engine() -> StateGraph:
    """Research Pipeline -> Extract Metrics -> Calculate Scores -> Aggregate ->
    Decision Report. Invalid input short-circuits straight to "aggregate" (which
    still publishes decision_failed and returns a well-formed, all-zero report),
    never running the Research Pipeline at all.
    """
    graph = StateGraph(DecisionEngineState)
    graph.add_node("intake", intake_node)
    graph.add_node("research", research_node)
    graph.add_node("extract_metrics", extract_metrics_node)
    graph.add_node("calculate_scores", calculate_scores_node)
    graph.add_node("aggregate", aggregate_node)

    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", _route_after_intake, ["research", "aggregate"])
    graph.add_conditional_edges("research", _route_after_research, ["extract_metrics", "aggregate"])
    graph.add_edge("extract_metrics", "calculate_scores")
    graph.add_edge("calculate_scores", "aggregate")
    graph.add_edge("aggregate", END)
    return graph


def compile_decision_engine() -> CompiledStateGraph:
    # Deliberately compiled without a checkpointer, same precedent as
    # workflows/research_pipeline.py's compile_research_pipeline() - this
    # workflow runs straight through with no approval gate.
    return build_decision_engine().compile()
