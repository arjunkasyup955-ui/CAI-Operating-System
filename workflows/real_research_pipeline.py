import logging
import threading
from typing import Any

import workflows.research_pipeline as research_pipeline
from agents.founder.research_pipeline.agent import run_research_pipeline
from agents.research.real_browser_research.agent import real_browser_node, real_competitor_intelligence_node
from core.execution_cache import ExecutionCacheManager, cached_workflow_invoker, get_default_cache_manager
from core.scheduler import JobContext, JobPriority, JobScheduler, register_job

logger = logging.getLogger("afos.workflows.real_research_pipeline")

# Serializes concurrent calls to run_real_research_pipeline() within this
# process, since the stage-function swap below is a process-global mutation
# of workflows.research_pipeline's module state (the exact same public DI
# seam scripts/smoke_test_phase4_research_pipeline.py already uses for
# fakes). Without this lock, two concurrent runs could interleave their
# swap/reset and briefly execute with the wrong stage functions.
_pipeline_lock = threading.Lock()

REAL_RESEARCH_JOB_TYPE = "real_research_pipeline"


def run_real_research_pipeline(
    idea: str,
    venture_id: str,
    research_depth: str = "standard",
    extra_stage_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Runs the existing, unmodified Phase 4 Research Pipeline
    (agents.founder.research_pipeline.agent.run_research_pipeline) with its
    'browser' and 'competitor_intelligence' stage slots temporarily swapped
    - via workflows.research_pipeline's own public set_stage_functions /
    reset_stage_functions seam - for this component's real, multi-source,
    competitor-discovery-and-enrichment versions. Every other stage (search,
    market_intelligence, trend_intelligence, opportunity_detection,
    idea_validation), the graph topology, retry-on-deep-depth semantics,
    event publishing, permission gating, and final report assembly are all
    reused completely unmodified.

    The swap is always reset in `finally`, even on exception, so a single
    call never leaves the Phase 4 pipeline permanently mutated for callers
    that don't go through this function.

    This function only ever ADDS fields to the resulting report - it never
    removes or reshapes an existing one - so the output stays a strict
    superset of what Founder Dashboard already expects (JSON-serializable,
    same top-level keys plus additions).

    `extra_stage_overrides` lets a caller (in practice, only this component's
    own tests) additionally swap in fast, deterministic stand-ins for the
    four downstream analysis stages this component doesn't own (market/
    trend/opportunity/idea_validation intelligence) - so a test can verify
    caching/scheduling/serialization behavior without also paying for five
    real LLM-timeout cascades on every single call. Defaults to None, which
    changes nothing about this function's production behavior.
    """
    with _pipeline_lock:
        overrides = {"browser": real_browser_node, "competitor_intelligence": real_competitor_intelligence_node}
        if extra_stage_overrides:
            overrides.update(extra_stage_overrides)
        research_pipeline.set_stage_functions(overrides)
        try:
            report = run_research_pipeline(idea, venture_id, research_depth)
        finally:
            research_pipeline.reset_stage_functions()

    browser_findings = report.get("browser_findings") or {}
    competitor_analysis = report.get("competitor_analysis") or {}
    report["competitor_comparison_table"] = (
        competitor_analysis.get("competitor_comparison_table") or browser_findings.get("competitor_comparison_table") or []
    )
    report["real_research_citations"] = sorted({
        *(browser_findings.get("citations") or []),
        *(competitor_analysis.get("citations") or []),
    })
    report["real_research"] = bool(browser_findings.get("real_research")) or bool(competitor_analysis.get("real_research"))
    logger.info(
        "real_research_pipeline: completed for venture_id=%s status=%s competitors=%d",
        venture_id, report.get("status"), len(report["competitor_comparison_table"]),
    )
    return report


def get_cached_real_research_invoker(cache_manager: ExecutionCacheManager | None = None, ttl_seconds: float | None = 1800.0):
    """Wraps run_real_research_pipeline with Phase 5 Component 1's Execution
    Cache (unmodified) - same cache-key derivation, same duplicate-execution
    guard, same hit/miss statistics every other cached workflow invoker in
    AFOS already gets.
    """
    manager = cache_manager or get_default_cache_manager()
    return cached_workflow_invoker("real_research_pipeline", run_real_research_pipeline, manager, ttl_seconds=ttl_seconds)


def _real_research_job_fn(
    ctx: JobContext,
    idea: str,
    venture_id: str,
    research_depth: str = "standard",
    use_cache: bool = True,
    extra_stage_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The Job function registered for REAL_RESEARCH_JOB_TYPE - see
    submit_real_research_job(). Reports progress/logs through JobContext so a
    long-running research run (multiple searches plus multiple page fetches)
    is observable and cooperatively cancellable via Phase 5 Component 2's
    scheduler, exactly like any other background job.

    `extra_stage_overrides` (only meaningful when use_cache=False, since the
    cached path's wrapped signature is fixed) is passed straight through to
    run_real_research_pipeline - see that function's own docstring.
    """
    ctx.log(f"real research job starting for idea={idea!r} venture_id={venture_id!r} depth={research_depth!r}")
    ctx.set_progress(5.0, "starting multi-source search and competitor discovery")
    ctx.check_cancelled()
    if use_cache:
        result = get_cached_real_research_invoker()(idea, venture_id, research_depth)
    else:
        result = run_real_research_pipeline(idea, venture_id, research_depth, extra_stage_overrides=extra_stage_overrides)
    ctx.check_cancelled()
    ctx.set_progress(100.0, f"completed with status={result.get('status')}")
    ctx.log(f"real research job finished with status={result.get('status')}, competitors={len(result.get('competitor_comparison_table', []))}")
    return result


def register_real_research_job() -> None:
    register_job(REAL_RESEARCH_JOB_TYPE, _real_research_job_fn)


def submit_real_research_job(
    scheduler: JobScheduler,
    idea: str,
    venture_id: str,
    research_depth: str = "standard",
    priority: int = JobPriority.NORMAL,
    max_retries: int = 2,
    retry_delay_seconds: float = 5.0,
    use_cache: bool = True,
    extra_stage_overrides: dict[str, Any] | None = None,
) -> str:
    """Submits a real research run as a background job on Phase 5 Component
    2's scheduler - the recommended way to run this component for anything
    beyond a quick synchronous call, since real multi-source search plus
    per-competitor page fetches can legitimately take a while ("support long-
    running founder tasks"). Automatic retry on temporary failure is handled
    at this layer by the scheduler's own max_retries/retry_delay_seconds,
    layered on top of (not replacing) the per-call retry already inside
    agents.research.real_browser_research.agent's _with_retries.
    """
    register_real_research_job()
    return scheduler.submit(
        REAL_RESEARCH_JOB_TYPE,
        args=[idea, venture_id, research_depth],
        kwargs={"use_cache": use_cache, "extra_stage_overrides": extra_stage_overrides},
        priority=priority,
        max_retries=max_retries,
        retry_delay_seconds=retry_delay_seconds,
    )
