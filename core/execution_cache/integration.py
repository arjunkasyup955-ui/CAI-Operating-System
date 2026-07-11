import logging
import time
from collections.abc import Callable
from typing import Any

from core.execution_cache.cache import ExecutionCacheManager

logger = logging.getLogger("afos.execution_cache")

WorkflowInvoker = Callable[[str, str, str], dict[str, Any]]

_default_cache_manager = ExecutionCacheManager()


def get_default_cache_manager() -> ExecutionCacheManager:
    return _default_cache_manager


def set_default_cache_manager(manager: ExecutionCacheManager) -> None:
    """Swappable, not cached - lets tests inject an isolated ExecutionCacheManager
    instance instead of sharing the process-wide default one.
    """
    global _default_cache_manager
    _default_cache_manager = manager


def reset_default_cache_manager() -> None:
    global _default_cache_manager
    _default_cache_manager = ExecutionCacheManager()


def cached_workflow_invoker(workflow_name: str, real_invoker: WorkflowInvoker, cache_manager: ExecutionCacheManager | None = None, ttl_seconds: float | None = None) -> WorkflowInvoker:
    """Wraps any (idea, venture_id, research_depth) -> dict workflow invoker -
    the exact signature every Phase 4 pipeline component's own default invoker
    already uses - with caching, duplicate-execution prevention, and execution
    metadata/history tracking. Returns a new callable with the identical
    signature, so it's a drop-in replacement through any Phase 4 component's
    own existing get_x_invoker()/set_x_invoker() dependency-injection seam.

    Never duplicates the wrapped workflow's own logic: on a cache miss, this
    calls `real_invoker` exactly once and caches whatever it returns, verbatim.
    """
    manager = cache_manager or get_default_cache_manager()

    def _invoker(idea: str, venture_id: str, research_depth: str = "standard") -> dict[str, Any]:
        cache_key = manager.make_cache_key(workflow_name, idea=idea, venture_id=venture_id, research_depth=research_depth)

        cached = manager.get(cache_key)
        if cached is not None:
            logger.info("execution_cache: cache hit for workflow '%s' (key=%s)", workflow_name, cache_key[:12])
            return {**cached, "cache_hit": True}

        if not manager.try_begin(cache_key):
            logger.info("execution_cache: duplicate execution prevented for workflow '%s' (key=%s)", workflow_name, cache_key[:12])
            return {
                "agent": workflow_name,
                "event": f"{workflow_name}_duplicate_execution_skipped",
                "status": "duplicate_execution",
                "idea": idea,
                "cache_hit": False,
            }

        try:
            start = time.monotonic()
            result = real_invoker(idea, venture_id, research_depth)
            elapsed = time.monotonic() - start
            manager.put(cache_key, workflow_name, result, elapsed, ttl_seconds=ttl_seconds)
            logger.info("execution_cache: cache miss for workflow '%s' (key=%s) - computed in %.3fs", workflow_name, cache_key[:12], elapsed)
            return {**result, "cache_hit": False}
        finally:
            manager.end(cache_key)

    return _invoker


def wire_cache_into_dashboard(
    cache_manager: ExecutionCacheManager | None = None,
    ttl_seconds: float | None = None,
    invokers: dict[str, WorkflowInvoker] | None = None,
) -> None:
    """Rewires the Founder Dashboard's own existing, already-public dependency-
    injection seam (workflows.founder_dashboard.set_component_invokers - the
    same seam that component's own smoke test already uses to inject fake
    invokers for testing) so every one of the 7 reused Phase 4 components'
    outputs is read from the Execution Cache instead of being recomputed.
    Satisfies "Founder Dashboard must read cached outputs instead of
    recomputing workflows" without editing a single Phase 4 file: this only
    *calls* Founder Dashboard's existing public API from here, it does not
    modify workflows/founder_dashboard.py in any way.

    `invokers` defaults to Founder Dashboard's own real, unmodified default
    invokers (the production case). Tests may pass a deterministic fake
    invoker set here instead, to verify the caching behavior itself - hit/miss,
    duplicate-execution prevention, statistics - without exercising the real,
    slow, network/LLM-dependent Phase 4 chain seven times over.
    """
    from workflows.founder_dashboard import _DEFAULT_COMPONENT_INVOKERS, set_component_invokers

    manager = cache_manager or get_default_cache_manager()
    base_invokers = invokers if invokers is not None else _DEFAULT_COMPONENT_INVOKERS
    wrapped = {name: cached_workflow_invoker(name, fn, manager, ttl_seconds=ttl_seconds) for name, fn in base_invokers.items()}
    set_component_invokers(wrapped)
    logger.info("execution_cache: wired caching into Founder Dashboard's %d component invokers", len(wrapped))


def unwire_cache_from_dashboard() -> None:
    """Restores the Founder Dashboard's component invokers to their real,
    uncached defaults - the mirror image of wire_cache_into_dashboard(), using
    Founder Dashboard's own existing reset_component_invokers() public API.
    """
    from workflows.founder_dashboard import reset_component_invokers

    reset_component_invokers()
    logger.info("execution_cache: unwired caching from Founder Dashboard")
