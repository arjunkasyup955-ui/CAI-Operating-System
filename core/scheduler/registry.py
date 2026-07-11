import threading
from collections.abc import Callable

_lock = threading.RLock()
_registry: dict[str, Callable] = {}


def register_job(job_type: str, fn: Callable) -> None:
    """Registers the callable a job_type name resolves to at execution time.

    Jobs are persisted (see JobScheduler.save_to_disk) as plain data - job_type
    name plus JSON-serializable args/kwargs - never as a pickled callable. This
    registry is what lets a restored job find its executable code again after a
    process restart: the new process just needs to have called register_job()
    for that job_type before starting the scheduler, exactly as it did before.
    """
    with _lock:
        _registry[job_type] = fn


def get_job_function(job_type: str) -> Callable | None:
    with _lock:
        return _registry.get(job_type)


def get_job_registry() -> dict[str, Callable]:
    with _lock:
        return dict(_registry)


def reset_job_registry() -> None:
    with _lock:
        _registry.clear()
