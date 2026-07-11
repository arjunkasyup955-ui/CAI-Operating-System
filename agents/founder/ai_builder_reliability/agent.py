import json
import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.registries import get_agent_registry
from tools.loop.loop_providers import LoopProvider, LoopStepResult

logger = logging.getLogger("afos.agents.ai_builder_reliability")

get_agent_registry().register_from_yaml(Path(__file__).parent / "manifest.yaml")

# A persistent, never-shut-down executor - the exact fix already established
# in core/registries/tool_registry.py and Phase 4 Component 5's own resilience
# wrapper for the `with ThreadPoolExecutor() as executor:` gotcha: the context
# manager's __exit__ blocks on shutdown(wait=True) until the submitted worker
# finishes, silently defeating any intended timeout.
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ai-builder-reliability")


# ========================================================================== #
# Failure classification
# ========================================================================== #

class FailureCategory:
    TIMEOUT = "timeout"
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    UNKNOWN = "unknown"


_CANCELLED_PATTERN = re.compile(r"build_cancelled", re.IGNORECASE)
_PAUSED_PATTERN = re.compile(r"build_paused", re.IGNORECASE)
_LOOP_RECOVERY_PATTERN = re.compile(r"loop_recovery", re.IGNORECASE)
_TIMEOUT_PATTERN = re.compile(r"timed?\s?out|timeout_exceeded|\btimeout\b", re.IGNORECASE)
_TRANSIENT_PATTERN = re.compile(r"connection|network|temporarily unavailable|rate limit|refused|unreachable|\b503\b|\b502\b|\b429\b", re.IGNORECASE)
_PERMANENT_PATTERN = re.compile(r"syntaxerror|importerror|unknown step type|validation|not found|permission denied|invalid", re.IGNORECASE)


def classify_failure(error_message: str | None) -> str:
    """Deterministic, keyword-based failure classification - no LLM call, so
    it's exhaustively unit-testable and always available even with no model
    configured. Order matters: cancellation/pause/loop-recovery are this
    module's own sentinel error strings (see ManagedLoopProvider) and are
    checked before the generic categories.
    """
    if not error_message:
        return FailureCategory.UNKNOWN
    if _CANCELLED_PATTERN.search(error_message):
        return FailureCategory.CANCELLED
    if _PAUSED_PATTERN.search(error_message):
        return FailureCategory.PAUSED
    if _LOOP_RECOVERY_PATTERN.search(error_message) or _TIMEOUT_PATTERN.search(error_message):
        return FailureCategory.TIMEOUT
    if _PERMANENT_PATTERN.search(error_message):
        return FailureCategory.PERMANENT
    if _TRANSIENT_PATTERN.search(error_message):
        return FailureCategory.TRANSIENT
    return FailureCategory.UNKNOWN


# ========================================================================== #
# Retry policy / exponential backoff
# ========================================================================== #

@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 5.0
    multiplier: float = 2.0


def compute_backoff_delay(attempt: int, policy: RetryPolicy) -> float:
    """attempt=1 is the first (non-retry) try, so no delay. attempt=2 is the
    first retry: delay = base * multiplier^0. attempt=3: base * multiplier^1.
    Capped at max_delay_seconds.
    """
    if attempt <= 1:
        return 0.0
    delay = policy.base_delay_seconds * (policy.multiplier ** (attempt - 2))
    return min(delay, policy.max_delay_seconds)


# ========================================================================== #
# Build Checkpoint Store - checkpoint save & resume / partial build resume
# ========================================================================== #

class BuildCheckpointStore:
    """Thread-safe, structured record of one managed build's progress, keyed
    by build_run_id. `completed_steps` is what makes "partial build resume"
    possible: ManagedLoopProvider checks this before doing any real work for
    a given step_index, so a fresh Loop Controller run (fresh 180s timeout
    budget, fresh retry_budget - both frozen Phase 4 constants) started
    against the SAME build_run_id skips every already-completed step nearly
    instantly and only spends real time/budget on what's actually left.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._checkpoints: dict[str, dict[str, Any]] = {}

    def create_checkpoint(self, build_run_id: str, venture_id: str, idea: str) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            checkpoint = {
                "build_run_id": build_run_id,
                "venture_id": venture_id,
                "idea": idea,
                "completed_steps": {},
                "current_step_index": 0,
                "status": "created",
                "pause_requested": False,
                "cancel_requested": False,
                "outer_attempts": 0,
                "last_failure_category": None,
                "created_at": now,
                "updated_at": now,
            }
            self._checkpoints[build_run_id] = checkpoint
            return dict(checkpoint)

    def get_checkpoint(self, build_run_id: str) -> dict[str, Any] | None:
        with self._lock:
            checkpoint = self._checkpoints.get(build_run_id)
            return json.loads(json.dumps(checkpoint)) if checkpoint is not None else None

    def record_step_result(self, build_run_id: str, step_index: int, output: str) -> None:
        with self._lock:
            checkpoint = self._checkpoints.get(build_run_id)
            if checkpoint is None:
                return
            checkpoint["completed_steps"][str(step_index)] = {"output": output, "completed_at": time.time()}
            checkpoint["current_step_index"] = max(checkpoint["current_step_index"], step_index + 1)
            checkpoint["updated_at"] = time.time()

    def is_step_completed(self, build_run_id: str, step_index: int) -> bool:
        with self._lock:
            checkpoint = self._checkpoints.get(build_run_id)
            return bool(checkpoint and str(step_index) in checkpoint["completed_steps"])

    def get_step_output(self, build_run_id: str, step_index: int) -> str | None:
        with self._lock:
            checkpoint = self._checkpoints.get(build_run_id)
            if not checkpoint:
                return None
            entry = checkpoint["completed_steps"].get(str(step_index))
            return entry["output"] if entry else None

    def request_pause(self, build_run_id: str) -> bool:
        with self._lock:
            checkpoint = self._checkpoints.get(build_run_id)
            if checkpoint is None or checkpoint["status"] in ("completed", "cancelled", "failed"):
                return False
            checkpoint["pause_requested"] = True
            checkpoint["updated_at"] = time.time()
            return True

    def request_resume(self, build_run_id: str) -> bool:
        with self._lock:
            checkpoint = self._checkpoints.get(build_run_id)
            if checkpoint is None:
                return False
            checkpoint["pause_requested"] = False
            checkpoint["updated_at"] = time.time()
            return True

    def request_cancel(self, build_run_id: str) -> bool:
        with self._lock:
            checkpoint = self._checkpoints.get(build_run_id)
            if checkpoint is None or checkpoint["status"] in ("completed", "cancelled", "failed"):
                return False
            checkpoint["cancel_requested"] = True
            checkpoint["updated_at"] = time.time()
            return True

    def is_pause_requested(self, build_run_id: str) -> bool:
        with self._lock:
            checkpoint = self._checkpoints.get(build_run_id)
            return bool(checkpoint and checkpoint["pause_requested"])

    def is_cancel_requested(self, build_run_id: str) -> bool:
        with self._lock:
            checkpoint = self._checkpoints.get(build_run_id)
            return bool(checkpoint and checkpoint["cancel_requested"])

    def mark_status(self, build_run_id: str, status: str, last_failure_category: str | None = None) -> None:
        with self._lock:
            checkpoint = self._checkpoints.get(build_run_id)
            if checkpoint is None:
                return
            checkpoint["status"] = status
            if last_failure_category is not None:
                checkpoint["last_failure_category"] = last_failure_category
            checkpoint["updated_at"] = time.time()

    def increment_outer_attempts(self, build_run_id: str) -> int:
        with self._lock:
            checkpoint = self._checkpoints.get(build_run_id)
            if checkpoint is None:
                return 0
            checkpoint["outer_attempts"] += 1
            checkpoint["updated_at"] = time.time()
            return checkpoint["outer_attempts"]

    def list_checkpoints(self, venture_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = [json.loads(json.dumps(c)) for c in self._checkpoints.values()]
        if venture_id is not None:
            items = [c for c in items if c["venture_id"] == venture_id]
        return items

    def delete_checkpoint(self, build_run_id: str) -> bool:
        with self._lock:
            return self._checkpoints.pop(build_run_id, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._checkpoints.clear()

    def save_to_disk(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            path.write_text(json.dumps(self._checkpoints, indent=2), encoding="utf-8")

    def load_from_disk(self, path: str | Path) -> bool:
        path = Path(path)
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        with self._lock:
            self._checkpoints = data
        return True


_default_checkpoint_store = BuildCheckpointStore()


def get_default_checkpoint_store() -> BuildCheckpointStore:
    return _default_checkpoint_store


def set_default_checkpoint_store(store: BuildCheckpointStore) -> None:
    global _default_checkpoint_store
    _default_checkpoint_store = store


def reset_default_checkpoint_store() -> None:
    global _default_checkpoint_store
    _default_checkpoint_store = BuildCheckpointStore()


# ========================================================================== #
# Reliability metrics
# ========================================================================== #

class ReliabilityMetrics:
    """Thread-safe counters for this component's own reliability behavior -
    distinct from (and complementary to) core.metrics, which aggregates
    execution-history-level stats. Feeds "Metrics collection" directly;
    "Integration with Production Dashboard" is achieved separately via
    recording each managed build into core.observability's
    ExecutionHistoryStore (see workflows/ai_builder_reliability.py), the same
    store Phase 5 Component 4's dashboard already reads from.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: dict[str, Any] = {
            "timeout_recoveries": 0,
            "step_retries": 0,
            "checkpoints_saved": 0,
            "builds_resumed": 0,
            "builds_cancelled": 0,
            "builds_paused": 0,
            "loop_recoveries": 0,
            "failure_categories": {},
        }

    def increment(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    def record_failure_category(self, category: str) -> None:
        with self._lock:
            categories = self._counters["failure_categories"]
            categories[category] = categories.get(category, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._counters))


_default_metrics = ReliabilityMetrics()


def get_default_reliability_metrics() -> ReliabilityMetrics:
    return _default_metrics


def reset_default_reliability_metrics() -> None:
    global _default_metrics
    _default_metrics = ReliabilityMetrics()


# ========================================================================== #
# Inner step-executor DI seam (which real LoopProvider ManagedLoopProvider
# delegates actual step work to - defaults to the real, unmodified
# AIBuilderLoopProvider from workflows.ai_builder)
# ========================================================================== #

def _real_inner_provider_factory(venture_id: str) -> LoopProvider:
    from workflows.ai_builder import AIBuilderLoopProvider

    return AIBuilderLoopProvider(venture_id)


InnerProviderFactory = Callable[[str], LoopProvider]

_inner_provider_factory: InnerProviderFactory = _real_inner_provider_factory


def get_inner_provider_factory() -> InnerProviderFactory:
    return _inner_provider_factory


def set_inner_provider_factory(fn: InnerProviderFactory) -> None:
    global _inner_provider_factory
    _inner_provider_factory = fn


def reset_inner_provider_factory() -> None:
    global _inner_provider_factory
    _inner_provider_factory = _real_inner_provider_factory


# ========================================================================== #
# ManagedLoopProvider
# ========================================================================== #

class ManagedLoopProvider:
    """A LoopProvider - installed into the AI Builder Pipeline's own
    documented extension point (workflows.ai_builder.set_step_executor_factory,
    the exact seam its own docstring describes as being for "a custom
    LoopProvider... swapped in through the same DI seam") - that wraps the
    real step executor with timeout enforcement, checkpoint-based resume,
    backoff retry, pause/cancel checks, and a loop-recovery tripwire.

    Critical constraint this design respects: tools/loop/loop.py (Phase 2,
    frozen) registers "execute_loop_step" - the tool that ends up calling
    execute_step() here - with its OWN 30-second Tool Registry timeout. Any
    single execute_step() call (including whatever internal work it does)
    MUST finish well under 30s, or the Tool Registry's blunt external timeout
    decides the outcome instead of this class's own more informative
    classification/checkpointing. That's why step_timeout_seconds defaults to
    20.0 (comfortably under 30s) and why retries-with-backoff happen ACROSS
    separate execute_step() invocations (each driven by the Loop Controller's
    own retry_budget re-invoking this method for the same step_index) rather
    than as a sleep-and-retry loop inside one call.

    Python cannot forcibly kill a running thread, so a per-step timeout here
    means "stop waiting and report a timeout", not "guarantee the underlying
    work actually stopped" - the same honest, cooperative-only limitation
    already documented in Phase 5 Component 2's JobScheduler.
    """

    name = "ai_builder_reliability_managed"

    def __init__(
        self,
        venture_id: str,
        build_run_id: str,
        checkpoint_store: BuildCheckpointStore | None = None,
        metrics: ReliabilityMetrics | None = None,
        inner_provider: LoopProvider | None = None,
        step_timeout_seconds: float = 20.0,
        retry_policy: RetryPolicy | None = None,
        pause_event: threading.Event | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.venture_id = venture_id
        self.build_run_id = build_run_id
        self.checkpoint_store = checkpoint_store or get_default_checkpoint_store()
        self.metrics = metrics or get_default_reliability_metrics()
        self.inner = inner_provider if inner_provider is not None else get_inner_provider_factory()(venture_id)
        self.step_timeout_seconds = step_timeout_seconds
        self.retry_policy = retry_policy or RetryPolicy()
        self.pause_event = pause_event or threading.Event()
        self.cancel_event = cancel_event or threading.Event()
        self._attempt_counts: dict[int, int] = {}
        self.debug_retry_invocations = 0

    def execute_step(self, step: dict[str, Any], step_index: int) -> LoopStepResult:
        if self.cancel_event.is_set() or self.checkpoint_store.is_cancel_requested(self.build_run_id):
            logger.info("ai_builder_reliability: step %d skipped - build %s cancelled", step_index, self.build_run_id)
            return LoopStepResult(success=False, step_index=step_index, error="build_cancelled")

        if self.pause_event.is_set() or self.checkpoint_store.is_pause_requested(self.build_run_id):
            logger.info("ai_builder_reliability: step %d skipped - build %s paused", step_index, self.build_run_id)
            return LoopStepResult(success=False, step_index=step_index, error="build_paused")

        if self.checkpoint_store.is_step_completed(self.build_run_id, step_index):
            cached_output = self.checkpoint_store.get_step_output(self.build_run_id, step_index)
            logger.info("ai_builder_reliability: step %d already checkpointed for build %s - resuming", step_index, self.build_run_id)
            return LoopStepResult(success=True, step_index=step_index, output=cached_output or "resumed from checkpoint")

        attempt = self._attempt_counts.get(step_index, 0) + 1
        self._attempt_counts[step_index] = attempt

        if attempt > self.retry_policy.max_attempts:
            logger.error(
                "ai_builder_reliability: loop_recovery triggered - step %d exceeded max attempts (%d) for build %s",
                step_index, self.retry_policy.max_attempts, self.build_run_id,
            )
            self.metrics.increment("loop_recoveries")
            self.metrics.record_failure_category(FailureCategory.TIMEOUT)
            return LoopStepResult(success=False, step_index=step_index, error="loop_recovery: max step attempts exceeded")

        if attempt > 1:
            delay = compute_backoff_delay(attempt, self.retry_policy)
            if delay > 0:
                logger.info("ai_builder_reliability: retrying step %d (attempt %d) after %.2fs backoff", step_index, attempt, delay)
                time.sleep(delay)
            self.metrics.increment("step_retries")

        future = _executor.submit(self.inner.execute_step, step, step_index)
        try:
            result = future.result(timeout=self.step_timeout_seconds)
        except FuturesTimeoutError:
            self.metrics.increment("timeout_recoveries")
            self.metrics.record_failure_category(FailureCategory.TIMEOUT)
            logger.warning("ai_builder_reliability: step %d timed out after %.1fs (build %s)", step_index, self.step_timeout_seconds, self.build_run_id)
            return LoopStepResult(success=False, step_index=step_index, error="timeout: step exceeded step_timeout_seconds")
        except Exception as exc:  # noqa: BLE001 - a step's real work may raise anything
            category = classify_failure(str(exc))
            self.metrics.record_failure_category(category)
            logger.warning("ai_builder_reliability: step %d raised (%s): %s", step_index, category, exc)
            return LoopStepResult(success=False, step_index=step_index, error=str(exc))

        if result.success:
            self.checkpoint_store.record_step_result(self.build_run_id, step_index, result.output)
            self.metrics.increment("checkpoints_saved")
            logger.info("ai_builder_reliability: step %d completed and checkpointed (build %s)", step_index, self.build_run_id)
            if isinstance(self.inner, object) and hasattr(self.inner, "debug_retry_invocations"):
                self.debug_retry_invocations = getattr(self.inner, "debug_retry_invocations", 0)
            return result

        category = classify_failure(result.error)
        self.metrics.record_failure_category(category)
        logger.warning("ai_builder_reliability: step %d failed (%s): %s", step_index, category, result.error)
        return result


# ========================================================================== #
# Active-build registry + pause/resume/cancel public API
# ========================================================================== #

_active_builds_lock = threading.RLock()
_active_builds: dict[str, ManagedLoopProvider] = {}


def register_active_build(build_run_id: str, provider: ManagedLoopProvider) -> None:
    with _active_builds_lock:
        _active_builds[build_run_id] = provider


def unregister_active_build(build_run_id: str) -> None:
    with _active_builds_lock:
        _active_builds.pop(build_run_id, None)


def pause_build(build_run_id: str) -> bool:
    """Requests a pause. The checkpoint store's pause_requested flag is the
    single, durable source of truth - both ManagedLoopProvider.execute_step()
    and run_managed_ai_builder()'s own outer wait loop check it directly, so
    a pause request is honored correctly whether or not a live
    ManagedLoopProvider instance happens to be registered at this exact
    instant (it usually isn't: the outer wrapper spends most of a pause
    window sitting in its own wait loop between Loop Controller runs, not
    inside one). Deliberately does NOT also poke a live provider's in-process
    threading.Event - that would create a second, harder-to-keep-in-sync
    signal for no real benefit, since the checkpoint-store check is already
    cheap (a dict lookup under a lock) and always reachable.
    """
    ok = get_default_checkpoint_store().request_pause(build_run_id)
    if ok:
        get_default_reliability_metrics().increment("builds_paused")
        logger.info("ai_builder_reliability: pause requested for build %s", build_run_id)
    return ok


def resume_build(build_run_id: str) -> bool:
    ok = get_default_checkpoint_store().request_resume(build_run_id)
    if ok:
        logger.info("ai_builder_reliability: resume requested for build %s", build_run_id)
    return ok


def cancel_build(build_run_id: str) -> bool:
    ok = get_default_checkpoint_store().request_cancel(build_run_id)
    with _active_builds_lock:
        provider = _active_builds.get(build_run_id)
        if provider is not None:
            provider.cancel_event.set()
    if ok:
        get_default_reliability_metrics().increment("builds_cancelled")
        logger.info("ai_builder_reliability: cancel requested for build %s", build_run_id)
    return ok


def get_build_status(build_run_id: str) -> dict[str, Any] | None:
    """Live build progress: reads the (thread-safe) checkpoint directly, so a
    caller on another thread can poll this while a build is still running.
    """
    return get_default_checkpoint_store().get_checkpoint(build_run_id)


# ========================================================================== #
# Build health monitoring
# ========================================================================== #

def compute_build_health(report: dict[str, Any]) -> dict[str, Any]:
    status = report.get("status", "unknown")
    steps_total = report.get("steps_total") or 0
    steps_completed = report.get("steps_completed") or 0
    completion_ratio = (steps_completed / steps_total) if steps_total else (1.0 if status == "completed" else 0.0)

    if status == "completed":
        health = "healthy"
    elif status == "cancelled":
        health = "degraded"
    elif completion_ratio >= 0.8:
        health = "degraded"
    else:
        health = "critical"

    return {
        "build_health": health,
        "completion_ratio": round(completion_ratio, 4),
        "outer_attempts": report.get("outer_attempts", 0),
        "failure_category": report.get("failure_category"),
        "timeout_recoveries": report.get("timeout_recoveries", 0),
    }
