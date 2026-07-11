import logging
from collections.abc import Callable
from typing import Any

from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents.research.browser.agent import browser_node
from agents.research.competitor_intelligence.agent import competitor_intelligence_node
from agents.research.idea_validation.agent import idea_validation_node
from agents.research.market_intelligence.agent import market_intelligence_node
from agents.research.opportunity_detection.agent import opportunity_detection_node
from agents.research.search.agent import search_node
from agents.research.trend_intelligence.agent import trend_intelligence_node
from core.event_bus import AFOSEvent, get_event_bus
from core.state import VentureState

logger = logging.getLogger("afos.workflows.research_pipeline")

StageFn = Callable[[VentureState], dict]

# Reuses the exact same node functions Phase 1's research_graph.py already wires -
# no agent logic is reimplemented here, only a new topology (no research_intake,
# per-stage events, depth-aware retry) purpose-built for this component.
_DEFAULT_STAGE_FUNCTIONS: dict[str, StageFn] = {
    "search": search_node,
    "browser": browser_node,
    "market_intelligence": market_intelligence_node,
    "competitor_intelligence": competitor_intelligence_node,
    "trend_intelligence": trend_intelligence_node,
    "opportunity_detection": opportunity_detection_node,
    "idea_validation": idea_validation_node,
}

_STAGE_ORDER = list(_DEFAULT_STAGE_FUNCTIONS)

_STAGE_AGENT_NAMES = {
    "search": "search_agent",
    "browser": "browser_agent",
    "market_intelligence": "market_intelligence_agent",
    "competitor_intelligence": "competitor_intelligence_agent",
    "trend_intelligence": "trend_intelligence_agent",
    "opportunity_detection": "opportunity_detection_agent",
    "idea_validation": "idea_validation_agent",
}

_FAILURE_EVENT_SUFFIXES = ("_failed", "_skipped", "_exception")

_stage_functions: dict[str, StageFn] = dict(_DEFAULT_STAGE_FUNCTIONS)


def get_stage_functions() -> dict[str, StageFn]:
    return _stage_functions


def set_stage_functions(overrides: dict[str, StageFn]) -> None:
    """Swappable, not cached - lets tests inject deterministic fake stage functions
    (any subset of the 7) instead of exercising the real, network/LLM-dependent
    workers. Same dependency-injection convention as every prior component's
    get_x_provider()/set_x_provider() pair, generalized here to per-stage
    granularity across all 7 reused agent node functions. Unspecified stages keep
    their default (real) function.
    """
    global _stage_functions
    _stage_functions = {**_DEFAULT_STAGE_FUNCTIONS, **overrides}


def reset_stage_functions() -> None:
    global _stage_functions
    _stage_functions = dict(_DEFAULT_STAGE_FUNCTIONS)


class ResearchPipelineState(VentureState, total=False):
    """Extends VentureState - per its own docstring ("Extend this, never create a
    parallel state shape") - with the fields this pipeline needs. Nothing here
    removes or repurposes an existing VentureState field.
    """

    research_depth: str
    report: dict[str, Any]


def _outcome_ok(entry: dict[str, Any]) -> bool:
    event = entry.get("event", "")
    return bool(event) and not any(event.endswith(suffix) for suffix in _FAILURE_EVENT_SUFFIXES)


def _run_stage(stage_name: str, state: ResearchPipelineState) -> dict:
    """Runs one reused agent node function with pipeline-level defensive wrapping:
    publishes started/completed events, retries once more (only) when
    research_depth == "deep" and the first attempt's own event marks it as
    failed/skipped, and never lets an exception from a stage escape - "gracefully
    continue if one analysis agent fails" is enforced here, layered on top of (not
    replacing) each agent's own internal try/except.
    """
    bus = get_event_bus()
    venture_id = state.get("venture_id", "")
    depth = state.get("research_depth", "standard")
    max_attempts = 2 if depth == "deep" else 1
    fn = get_stage_functions()[stage_name]

    bus.publish(
        AFOSEvent(type="research_pipeline_stage_started", source_agent="research_pipeline", venture_id=venture_id, payload={"stage": stage_name})
    )

    delta: dict[str, Any] = {}
    last_entry: dict[str, Any] = {}
    for attempt in range(1, max_attempts + 1):
        try:
            delta = fn(state)
            entries = delta.get("research_findings") or delta.get("history") or []
            last_entry = entries[-1] if entries else {"agent": _STAGE_AGENT_NAMES[stage_name], "event": f"{stage_name}_unknown"}
            if _outcome_ok(last_entry) or attempt == max_attempts:
                break
            logger.info("research_pipeline: stage '%s' outcome '%s' on attempt %d, retrying (research_depth=deep)", stage_name, last_entry.get("event"), attempt)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            logger.warning("research_pipeline: stage '%s' raised on attempt %d: %s", stage_name, attempt, exc)
            last_entry = {"agent": _STAGE_AGENT_NAMES[stage_name], "event": f"{stage_name}_stage_exception", "error": str(exc)}
            delta = {"research_findings": [last_entry], "history": [last_entry]}
            if attempt == max_attempts:
                break

    bus.publish(
        AFOSEvent(
            type="research_pipeline_stage_completed", source_agent="research_pipeline", venture_id=venture_id,
            payload={"stage": stage_name, "outcome": last_entry.get("event"), "ok": _outcome_ok(last_entry)},
        )
    )
    return delta


def _make_stage_node(stage_name: str) -> Callable[[ResearchPipelineState], dict]:
    def _node(state: ResearchPipelineState) -> dict:
        return _run_stage(stage_name, state)

    _node.__name__ = f"{stage_name}_pipeline_stage"
    return _node


def _collect(findings: list[dict[str, Any]], agent_name: str) -> dict[str, Any]:
    matches = [f for f in findings if f.get("agent") == agent_name]
    return matches[-1] if matches else {}


def report_node(state: ResearchPipelineState) -> dict:
    """Assembles the final FounderResearchReport from everything the 7 reused
    stages already wrote to research_findings - no new agent calls here, pure
    aggregation. Best-effort stores a summary in the Memory Gateway's vector tier
    (semantic recall for later research) - never lets that failure (e.g. no
    OPENAI_API_KEY configured for embeddings) affect the report itself.
    """
    findings = state.get("research_findings", [])
    venture_id = state.get("venture_id", "")

    search_results = _collect(findings, "search_agent")
    browser_findings = _collect(findings, "browser_agent")
    market_analysis = _collect(findings, "market_intelligence_agent")
    competitor_analysis = _collect(findings, "competitor_intelligence_agent")
    trend_analysis = _collect(findings, "trend_intelligence_agent")
    opportunity_analysis = _collect(findings, "opportunity_detection_agent")
    idea_validation = _collect(findings, "idea_validation_agent")

    sources: list[str] = []
    for entry in (market_analysis, competitor_analysis, trend_analysis, opportunity_analysis, idea_validation):
        for source in entry.get("sources_used", []) or []:
            if source not in sources:
                sources.append(source)
    if browser_findings.get("url") and browser_findings["url"] not in sources:
        sources.append(browser_findings["url"])

    stage_results = [
        {
            "stage": stage,
            "agent": _STAGE_AGENT_NAMES[stage],
            "outcome": _collect(findings, _STAGE_AGENT_NAMES[stage]).get("event", "not_run"),
            "ok": _outcome_ok(_collect(findings, _STAGE_AGENT_NAMES[stage])),
        }
        for stage in _STAGE_ORDER
    ]
    ok_count = sum(1 for s in stage_results if s["ok"])

    if idea_validation.get("event") == "idea_validation_completed" and "confidence_score" in idea_validation:
        confidence_score = float(idea_validation["confidence_score"])
    else:
        confidence_score = round(ok_count / len(stage_results), 4) if stage_results else 0.0

    status = "completed" if ok_count > 0 else "no_data"

    report = {
        "idea": state.get("idea", ""),
        "venture_id": venture_id,
        "research_depth": state.get("research_depth", "standard"),
        "search_results": search_results,
        "browser_findings": browser_findings,
        "market_analysis": market_analysis,
        "competitor_analysis": competitor_analysis,
        "trend_analysis": trend_analysis,
        "opportunity_analysis": opportunity_analysis,
        "idea_validation": idea_validation,
        "sources": sources,
        "confidence_score": confidence_score,
        "stage_results": stage_results,
        "status": status,
    }

    # Best-effort only, and only attempted when embeddings can actually succeed:
    # VectorStore.add() (frozen kernel code) INSERTs a row, then calls the embed
    # function, then commits - if the embed call fails partway through (e.g. no
    # OPENAI_API_KEY, the default in this sandbox), the INSERT is left
    # uncommitted on the Memory Gateway's single long-lived sqlite3 connection,
    # which then blocks any other process (including a subprocess-spawned
    # regression test) trying to write to the same database file. Checking the
    # API key first avoids the partial write in the common (no-key) case; the
    # rollback in `except` is a safety net for any other failure mode.
    from core.config import get_settings

    if get_settings().openai_api_key:
        try:
            from core.memory_gateway import get_memory_gateway

            summary = f"Research report for '{report['idea']}' (venture {venture_id}): status={status}, confidence={confidence_score}, sources={len(sources)}"
            get_memory_gateway().vector.add(summary, venture_id=venture_id, kind="research_report", metadata={"status": status, "confidence_score": confidence_score})
        except Exception as exc:
            logger.info("research_pipeline: could not store report in vector memory (non-fatal): %s", exc)
            try:
                get_memory_gateway().knowledge_conn.rollback()
            except Exception:
                pass

    get_event_bus().publish(
        AFOSEvent(type="research_pipeline_completed", source_agent="research_pipeline", venture_id=venture_id, payload={"status": status, "confidence_score": confidence_score})
    )

    return {
        "report": report,
        "history": [{"agent": "research_pipeline", "event": "research_pipeline_completed", "status": status}],
    }


def build_research_pipeline() -> StateGraph:
    """Search -> Browser -> Market/Competitor/Trend Intelligence -> Opportunity
    Detection -> Idea Validation -> Report. Every stage node reuses an existing,
    unmodified agent node function; "research_intake" (the Research Supervisor's
    own stub) is intentionally not part of this pipeline - Component 2's stage list
    starts at Search Agent.
    """
    graph = StateGraph(ResearchPipelineState)
    for stage in _STAGE_ORDER:
        graph.add_node(stage, _make_stage_node(stage))
    graph.add_node("report", report_node)

    graph.add_edge(START, _STAGE_ORDER[0])
    for current, following in zip(_STAGE_ORDER, _STAGE_ORDER[1:]):
        graph.add_edge(current, following)
    graph.add_edge(_STAGE_ORDER[-1], "report")
    graph.add_edge("report", END)
    return graph


def compile_research_pipeline() -> CompiledStateGraph:
    # Deliberately compiled without a checkpointer, same precedent as
    # workflows/research_graph.py's compile_research_graph() - this pipeline runs
    # straight through with no approval gate, so nothing needs to pause/resume.
    return build_research_pipeline().compile()
