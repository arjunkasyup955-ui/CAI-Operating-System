import heapq
import itertools
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.scheduler.jobs import TERMINAL_STATUSES, Job, JobCancelledError, JobContext, JobPriority, JobStatus
from core.scheduler.registry import get_job_function


@dataclass
class RecurringJobSpec:
    """A standing instruction to submit a fresh Job of `job_type` every
    `interval_seconds`. Each firing creates an independent Job with its own
    job_id, retry policy, logs, and history entry - the spec itself is never
    executed directly.
    """

    spec_id: str
    job_type: str
    args: list = field(default_factory=list)
    kwargs: dict = field(default_factory=dict)
    priority: int = int(JobPriority.NORMAL)
    interval_seconds: float = 60.0
    max_retries: int = 0
    retry_delay_seconds: float = 5.0
    next_run_at: float = 0.0
    run_count: int = 0
    created_at: float = 0.0


class JobScheduler:
    """Production-grade, thread-safe background job scheduler for AFOS.

    A single re-entrant lock (wrapped in a Condition for worker wake-up)
    guards every mutable structure: the job table, the priority queue, the
    in-progress cancellation set, and the recurring-job specs. Workers pull
    from a heap ordered by (-priority, submission sequence), so higher
    JobPriority always runs before lower, and ties are broken FIFO. Jobs
    delayed by a retry backoff are lazily skipped and re-pushed by whichever
    worker next encounters them, rather than tracked with a separate timer
    thread per job.

    Job functions are plain callables registered once via
    core.scheduler.registry.register_job(job_type, fn) and invoked as
    fn(ctx: JobContext, *args, **kwargs). This indirection (job_type name,
    not a pickled function) is what makes persistence to disk possible.
    """

    def __init__(self, num_workers: int = 4, tick_interval_seconds: float = 0.2) -> None:
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._jobs: dict[str, Job] = {}
        self._queue: list[tuple[int, int, str]] = []
        self._queued_ids: set[str] = set()
        self._seq = itertools.count()
        self._cancel_requested: set[str] = set()
        self._recurring_specs: dict[str, RecurringJobSpec] = {}
        self._num_workers = num_workers
        self._tick_interval_seconds = tick_interval_seconds
        self._workers: list[threading.Thread] = []
        self._ticker: threading.Thread | None = None
        self._running = False
        self._paused_scheduler = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        with self._condition:
            if self._running:
                return
            self._running = True
            self._workers = [
                threading.Thread(target=self._worker_loop, name=f"afos-scheduler-worker-{i}", daemon=True)
                for i in range(self._num_workers)
            ]
            for worker in self._workers:
                worker.start()
            self._ticker = threading.Thread(target=self._recurring_loop, name="afos-scheduler-recurring-ticker", daemon=True)
            self._ticker.start()

    def shutdown(self, wait: bool = True, timeout: float = 5.0) -> None:
        with self._condition:
            self._running = False
            self._condition.notify_all()
        if wait:
            for worker in self._workers:
                worker.join(timeout=timeout)
            if self._ticker is not None:
                self._ticker.join(timeout=timeout)

    def pause(self) -> None:
        with self._condition:
            self._paused_scheduler = True

    def resume(self) -> None:
        with self._condition:
            self._paused_scheduler = False
            self._condition.notify_all()

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused_scheduler

    # ------------------------------------------------------------------ #
    # Submission
    # ------------------------------------------------------------------ #

    def submit(
        self,
        job_type: str,
        args: list | None = None,
        kwargs: dict | None = None,
        priority: int = JobPriority.NORMAL,
        max_retries: int = 0,
        retry_delay_seconds: float = 5.0,
        job_id: str | None = None,
    ) -> str:
        with self._condition:
            job = Job(
                job_id=job_id or str(uuid.uuid4()),
                job_type=job_type,
                args=list(args or []),
                kwargs=dict(kwargs or {}),
                priority=int(priority),
                status=JobStatus.QUEUED.value,
                max_retries=max_retries,
                retry_delay_seconds=retry_delay_seconds,
                created_at=time.time(),
                updated_at=time.time(),
            )
            self._jobs[job.job_id] = job
            self._enqueue_locked(job)
            return job.job_id

    def _enqueue_locked(self, job: Job) -> None:
        seq = next(self._seq)
        heapq.heappush(self._queue, (-job.priority, seq, job.job_id))
        self._queued_ids.add(job.job_id)
        self._condition.notify_all()

    # ------------------------------------------------------------------ #
    # Cancellation, pause/resume of individual jobs
    # ------------------------------------------------------------------ #

    def cancel(self, job_id: str) -> bool:
        with self._condition:
            job = self._jobs.get(job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                return False
            if job.status in (JobStatus.QUEUED.value, JobStatus.PAUSED.value):
                job.status = JobStatus.CANCELLED.value
                job.completed_at = time.time()
                job.updated_at = job.completed_at
                self._queued_ids.discard(job_id)
                return True
            if job.status == JobStatus.RUNNING.value:
                self._cancel_requested.add(job_id)
                return True
            return False

    def pause_job(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != JobStatus.QUEUED.value:
                return False
            job.status = JobStatus.PAUSED.value
            job.updated_at = time.time()
            return True

    def resume_job(self, job_id: str) -> bool:
        with self._condition:
            job = self._jobs.get(job_id)
            if job is None or job.status != JobStatus.PAUSED.value:
                return False
            job.status = JobStatus.QUEUED.value
            job.next_retry_at = None
            job.updated_at = time.time()
            self._enqueue_locked(job)
            return True

    # ------------------------------------------------------------------ #
    # Worker loop
    # ------------------------------------------------------------------ #

    def _worker_loop(self) -> None:
        while True:
            job = None
            with self._condition:
                while True:
                    if not self._running:
                        return
                    if not self.is_paused() and self._queue:
                        job = self._pop_ready_job_locked()
                        if job is not None:
                            break
                    self._condition.wait(timeout=self._tick_interval_seconds)
                    if not self._running:
                        return
            self._run_job(job)

    def _pop_ready_job_locked(self) -> Job | None:
        deferred: list[tuple[int, int, str]] = []
        ready: Job | None = None
        now = time.time()
        while self._queue:
            entry = heapq.heappop(self._queue)
            job_id = entry[2]
            self._queued_ids.discard(job_id)
            job = self._jobs.get(job_id)
            if job is None or job.status != JobStatus.QUEUED.value:
                continue
            if job.next_retry_at and job.next_retry_at > now:
                deferred.append(entry)
                continue
            job.status = JobStatus.RUNNING.value
            job.started_at = now
            job.updated_at = now
            ready = job
            break
        for entry in deferred:
            heapq.heappush(self._queue, entry)
            self._queued_ids.add(entry[2])
        return ready

    def _run_job(self, job: Job) -> None:
        fn = get_job_function(job.job_type)
        ctx = JobContext(job.job_id, self)
        self._append_log(job.job_id, f"started (attempt {job.attempts + 1})")
        try:
            if fn is None:
                raise RuntimeError(f"no job function registered for job_type '{job.job_type}'")
            result = fn(ctx, *job.args, **job.kwargs)
            with self._condition:
                current = self._jobs[job.job_id]
                if current.status != JobStatus.CANCELLED.value:
                    current.status = JobStatus.COMPLETED.value
                    current.result = result
                    current.completed_at = time.time()
                    current.updated_at = current.completed_at
                self._cancel_requested.discard(job.job_id)
            self._append_log(job.job_id, "completed successfully")
        except JobCancelledError:
            with self._condition:
                current = self._jobs[job.job_id]
                current.status = JobStatus.CANCELLED.value
                current.completed_at = time.time()
                current.updated_at = current.completed_at
                self._cancel_requested.discard(job.job_id)
            self._append_log(job.job_id, "cancelled cooperatively")
        except Exception as exc:
            with self._condition:
                current = self._jobs[job.job_id]
                current.attempts += 1
                current.error = f"{type(exc).__name__}: {exc}"
                current.updated_at = time.time()
                self._cancel_requested.discard(job.job_id)
                if current.attempts <= current.max_retries:
                    current.status = JobStatus.QUEUED.value
                    current.next_retry_at = time.time() + current.retry_delay_seconds
                    self._enqueue_locked(current)
                else:
                    current.status = JobStatus.FAILED.value
                    current.completed_at = time.time()
            self._append_log(job.job_id, f"attempt failed: {exc}")

    # ------------------------------------------------------------------ #
    # Execution logs / progress / cancellation flag (used by JobContext)
    # ------------------------------------------------------------------ #

    def _append_log(self, job_id: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.logs.append({"timestamp": time.time(), "message": message})
            job.updated_at = time.time()

    def _set_progress(self, job_id: str, percent: float, message: str = "") -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.progress_percent = max(0.0, min(100.0, float(percent)))
            job.progress_message = message
            job.updated_at = time.time()

    def _is_cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancel_requested

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return Job.from_dict(job.to_dict()) if job is not None else None

    def get_status(self, job_id: str) -> str | None:
        job = self.get_job(job_id)
        return job.status if job is not None else None

    def get_progress(self, job_id: str) -> tuple[float, str] | None:
        job = self.get_job(job_id)
        return (job.progress_percent, job.progress_message) if job is not None else None

    def get_logs(self, job_id: str) -> list[dict[str, Any]]:
        job = self.get_job(job_id)
        return job.logs if job is not None else []

    def get_result(self, job_id: str) -> Any:
        job = self.get_job(job_id)
        return job.result if job is not None else None

    def list_jobs(self, status: str | None = None, job_type: str | None = None) -> list[Job]:
        with self._lock:
            items = list(self._jobs.values())
        if status is not None:
            items = [j for j in items if j.status == status]
        if job_type is not None:
            items = [j for j in items if j.job_type == job_type]
        return [Job.from_dict(j.to_dict()) for j in items]

    def history(self, status: str | None = None, job_type: str | None = None) -> list[Job]:
        with self._lock:
            items = [j for j in self._jobs.values() if j.status in TERMINAL_STATUSES]
        if status is not None:
            items = [j for j in items if j.status == status]
        if job_type is not None:
            items = [j for j in items if j.job_type == job_type]
        items.sort(key=lambda j: j.completed_at or 0.0, reverse=True)
        return [Job.from_dict(j.to_dict()) for j in items]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            by_status: dict[str, int] = {}
            for job in self._jobs.values():
                by_status[job.status] = by_status.get(job.status, 0) + 1
            return {
                "total_jobs": len(self._jobs),
                "queue_size": len(self._queue),
                "by_status": by_status,
                "worker_count": self._num_workers,
                "paused": self.is_paused(),
                "recurring_spec_count": len(self._recurring_specs),
            }

    def wait_for(self, job_id: str, timeout: float | None = None, poll_interval: float = 0.02) -> Job | None:
        """Polls until `job_id` reaches a terminal status or `timeout` elapses.
        A convenience for callers (including tests) that need to block on a
        submitted job without duplicating scheduler internals.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            job = self.get_job(job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                return job
            if deadline is not None and time.monotonic() >= deadline:
                return job
            time.sleep(poll_interval)

    # ------------------------------------------------------------------ #
    # Failed job recovery
    # ------------------------------------------------------------------ #

    def recover_failed_jobs(self, reset_attempts: bool = True) -> int:
        with self._condition:
            recovered = 0
            for job in self._jobs.values():
                if job.status != JobStatus.FAILED.value:
                    continue
                job.status = JobStatus.QUEUED.value
                job.error = None
                job.next_retry_at = None
                if reset_attempts:
                    job.attempts = 0
                job.updated_at = time.time()
                self._enqueue_locked(job)
                recovered += 1
            return recovered

    # ------------------------------------------------------------------ #
    # Scheduled recurring jobs
    # ------------------------------------------------------------------ #

    def schedule_recurring(
        self,
        job_type: str,
        interval_seconds: float,
        args: list | None = None,
        kwargs: dict | None = None,
        priority: int = JobPriority.NORMAL,
        max_retries: int = 0,
        retry_delay_seconds: float = 5.0,
        run_immediately: bool = True,
        spec_id: str | None = None,
    ) -> str:
        with self._lock:
            now = time.time()
            spec = RecurringJobSpec(
                spec_id=spec_id or str(uuid.uuid4()),
                job_type=job_type,
                args=list(args or []),
                kwargs=dict(kwargs or {}),
                priority=int(priority),
                interval_seconds=interval_seconds,
                max_retries=max_retries,
                retry_delay_seconds=retry_delay_seconds,
                next_run_at=now if run_immediately else now + interval_seconds,
                created_at=now,
            )
            self._recurring_specs[spec.spec_id] = spec
            return spec.spec_id

    def cancel_recurring(self, spec_id: str) -> bool:
        with self._lock:
            return self._recurring_specs.pop(spec_id, None) is not None

    def get_recurring_spec(self, spec_id: str) -> RecurringJobSpec | None:
        with self._lock:
            spec = self._recurring_specs.get(spec_id)
            return RecurringJobSpec(**asdict(spec)) if spec is not None else None

    def _recurring_loop(self) -> None:
        while True:
            with self._condition:
                if not self._running:
                    return
            self._fire_due_recurring_specs()
            with self._condition:
                if not self._running:
                    return
                self._condition.wait(timeout=self._tick_interval_seconds)

    def _fire_due_recurring_specs(self) -> None:
        with self._condition:
            now = time.time()
            for spec in self._recurring_specs.values():
                if spec.next_run_at > now:
                    continue
                job = Job(
                    job_id=str(uuid.uuid4()),
                    job_type=spec.job_type,
                    args=list(spec.args),
                    kwargs=dict(spec.kwargs),
                    priority=spec.priority,
                    status=JobStatus.QUEUED.value,
                    max_retries=spec.max_retries,
                    retry_delay_seconds=spec.retry_delay_seconds,
                    created_at=now,
                    updated_at=now,
                    recurrence_parent_id=spec.spec_id,
                )
                self._jobs[job.job_id] = job
                self._enqueue_locked(job)
                spec.run_count += 1
                spec.next_run_at = now + spec.interval_seconds

    # ------------------------------------------------------------------ #
    # Persistent job state / resume after interruption
    # ------------------------------------------------------------------ #

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "jobs": {job_id: job.to_dict() for job_id, job in self._jobs.items()},
                "recurring_specs": {spec_id: asdict(spec) for spec_id, spec in self._recurring_specs.items()},
            }

    def restore(self, snapshot: dict[str, Any]) -> int:
        """Restores jobs and recurring specs from a snapshot. Any job that was
        RUNNING at snapshot time could not have actually finished (this process
        wasn't alive to see it finish), so it is reset to QUEUED and
        re-enqueued rather than left stuck - this is the "resume after
        interruption" / "failed job recovery" guarantee. Returns the number of
        jobs recovered from an interrupted RUNNING state.
        """
        with self._condition:
            self._jobs = {job_id: Job.from_dict(data) for job_id, data in snapshot.get("jobs", {}).items()}
            self._recurring_specs = {
                spec_id: RecurringJobSpec(**data) for spec_id, data in snapshot.get("recurring_specs", {}).items()
            }
            self._queue = []
            self._queued_ids = set()
            self._cancel_requested = set()
            recovered = 0
            for job in self._jobs.values():
                if job.status == JobStatus.RUNNING.value:
                    job.status = JobStatus.QUEUED.value
                    job.next_retry_at = None
                    job.updated_at = time.time()
                    recovered += 1
                if job.status == JobStatus.QUEUED.value:
                    self._enqueue_locked(job)
            return recovered

    def save_to_disk(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.snapshot(), indent=2), encoding="utf-8")

    def load_from_disk(self, path: str | Path) -> bool:
        path = Path(path)
        if not path.exists():
            return False
        self.restore(json.loads(path.read_text(encoding="utf-8")))
        return True
