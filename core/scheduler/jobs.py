import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class JobPriority(int, Enum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}


class JobCancelledError(Exception):
    """Raised by JobContext.check_cancelled() so a cooperating job function can
    unwind cleanly; caught by the scheduler's worker loop and mapped to a
    CANCELLED terminal status.
    """

    def __init__(self, job_id: str) -> None:
        super().__init__(f"job {job_id} was cancelled")
        self.job_id = job_id


@dataclass
class Job:
    """A single unit of scheduled work. Fully JSON-serializable (to_dict/
    from_dict) except for `result`, which is only as serializable as whatever
    the job function returns - the same limitation any disk-persisted job
    queue has.
    """

    job_id: str
    job_type: str
    args: list = field(default_factory=list)
    kwargs: dict = field(default_factory=dict)
    priority: int = int(JobPriority.NORMAL)
    status: str = JobStatus.QUEUED.value
    max_retries: int = 0
    retry_delay_seconds: float = 5.0
    attempts: int = 0
    progress_percent: float = 0.0
    progress_message: str = ""
    logs: list = field(default_factory=list)
    result: Any = None
    error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float | None = None
    completed_at: float | None = None
    next_retry_at: float | None = None
    recurrence_parent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        return cls(**data)


class JobContext:
    """Passed as the first argument to every registered job function, giving
    it a narrow, thread-safe channel back into the scheduler for logging,
    progress reporting, and cooperative cancellation - without handing it the
    scheduler itself (and therefore the ability to submit/cancel unrelated
    jobs from inside a running job).
    """

    def __init__(self, job_id: str, scheduler: Any) -> None:
        self.job_id = job_id
        self._scheduler = scheduler

    def log(self, message: str) -> None:
        self._scheduler._append_log(self.job_id, message)

    def set_progress(self, percent: float, message: str = "") -> None:
        self._scheduler._set_progress(self.job_id, percent, message)

    def is_cancelled(self) -> bool:
        return self._scheduler._is_cancel_requested(self.job_id)

    def check_cancelled(self) -> None:
        if self.is_cancelled():
            raise JobCancelledError(self.job_id)


def now() -> float:
    return time.time()
