import uuid
from typing import Any, Protocol

from pydantic import BaseModel

# Unlike every other Phase 3 tool module, this one deliberately DOES read from the
# frozen Phase 0 kernel's core.config.Settings instead of its own env parsing -
# `langchain_tracing_v2`, `langchain_api_key`, and `langchain_project` already exist
# there specifically for LangSmith, since LangSmith is the kernel's own trace
# backend (see CLAUDE.md). Duplicating that config here with different env var
# names would drift from the one source of truth the kernel already established.
# This is read-only reuse, not a kernel modification (same reasoning as Chroma
# reusing core/memory_gateway's `_openai_embed` in Phase 3 Component 3).
from core.config import get_settings


class LangSmithHealthStatus(BaseModel):
    healthy: bool
    configured: bool
    project: str = ""
    error: str = ""


class LangSmithOperationResult(BaseModel):
    success: bool
    operation: str
    trace_id: str = ""
    run_id: str = ""
    dataset_id: str = ""
    feedback_id: str = ""
    error: str = ""


class LangSmithProvider(Protocol):
    """Tracing/observability connectivity. Every method either returns a
    LangSmithOperationResult on success or raises on failure - the agent layer's
    try/except (with ToolRegistry's RetryPolicy) handles graceful degradation
    uniformly, the same pattern as every prior provider. Field names deliberately
    avoid "name" anywhere (ToolRegistry.invoke()'s own positional parameter is
    literally called `name` - a schema field called `name` collides with it; found
    and fixed in the Chroma component, Phase 3 Component 3; avoided proactively
    here via "trace_name"/"run_name"/"dataset_name").
    """

    name: str

    def health_check(self) -> LangSmithHealthStatus: ...
    def create_trace(self, trace_name: str, inputs: dict[str, Any] | None = None) -> LangSmithOperationResult: ...
    def update_trace(self, trace_id: str, metadata: dict[str, Any] | None = None) -> LangSmithOperationResult: ...
    def end_trace(self, trace_id: str, outputs: dict[str, Any] | None = None, error: str | None = None) -> LangSmithOperationResult: ...
    def create_run(self, trace_id: str, run_name: str, run_type: str = "chain", inputs: dict[str, Any] | None = None) -> LangSmithOperationResult: ...
    def log_metrics(self, run_id: str, metrics: dict[str, float]) -> LangSmithOperationResult: ...
    def log_feedback(self, run_id: str, feedback_key: str, score: float | None = None, comment: str = "") -> LangSmithOperationResult: ...
    def dataset_upload(self, dataset_name: str, examples: list[dict[str, Any]]) -> LangSmithOperationResult: ...


class LangSmithSDKProvider:
    """Default LangSmithProvider - real integration via the `langsmith` SDK
    (already installed in this environment per CLAUDE.md, unlike psycopg/chromadb/
    docker-py/crawl4ai/playwright), lazily imported anyway for the same defensive
    posture as every other real provider, and additionally guarded on a configured
    API key - the SDK itself doesn't raise ImportError here (it's installed), but
    every call still gracefully surfaces "not configured" or connectivity/auth
    failures rather than crashing.
    """

    name = "langsmith_sdk"

    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if self._client is None:
            settings = get_settings()
            if not settings.langchain_api_key:
                raise RuntimeError("LANGCHAIN_API_KEY is not configured")
            from langsmith import Client

            self._client = Client(api_key=settings.langchain_api_key)
        return self._client

    def health_check(self) -> LangSmithHealthStatus:
        settings = get_settings()
        if not settings.langchain_api_key:
            return LangSmithHealthStatus(healthy=False, configured=False, project=settings.langchain_project, error="LANGCHAIN_API_KEY is not configured")
        try:
            client = self._get_client()
            next(iter(client.list_projects(limit=1)), None)
            return LangSmithHealthStatus(healthy=True, configured=True, project=settings.langchain_project)
        except Exception as exc:
            return LangSmithHealthStatus(healthy=False, configured=True, project=settings.langchain_project, error=str(exc))

    # NOTE: create_trace/create_run below model a trace as a root run and its
    # children as child runs, per LangSmith's own run-tree model
    # (Client.create_run(..., trace_id=..., parent_run_id=...)). This exact call
    # shape is version-sensitive and unverified against a live, authenticated
    # LangSmith account in this sandbox (no API key configured) - adjust if your
    # langsmith SDK version's signature differs.

    def create_trace(self, trace_name: str, inputs: dict[str, Any] | None = None) -> LangSmithOperationResult:
        client = self._get_client()
        run_id = uuid.uuid4()
        client.create_run(name=trace_name, run_type="chain", inputs=inputs or {}, id=run_id, trace_id=run_id)
        return LangSmithOperationResult(success=True, operation="create_trace", trace_id=str(run_id))

    def update_trace(self, trace_id: str, metadata: dict[str, Any] | None = None) -> LangSmithOperationResult:
        client = self._get_client()
        client.update_run(run_id=trace_id, extra={"metadata": metadata or {}})
        return LangSmithOperationResult(success=True, operation="update_trace", trace_id=trace_id)

    def end_trace(self, trace_id: str, outputs: dict[str, Any] | None = None, error: str | None = None) -> LangSmithOperationResult:
        client = self._get_client()
        client.update_run(run_id=trace_id, outputs=outputs or {}, error=error)
        return LangSmithOperationResult(success=True, operation="end_trace", trace_id=trace_id)

    def create_run(self, trace_id: str, run_name: str, run_type: str = "chain", inputs: dict[str, Any] | None = None) -> LangSmithOperationResult:
        client = self._get_client()
        run_id = uuid.uuid4()
        client.create_run(name=run_name, run_type=run_type, inputs=inputs or {}, id=run_id, trace_id=trace_id, parent_run_id=trace_id)
        return LangSmithOperationResult(success=True, operation="create_run", run_id=str(run_id), trace_id=trace_id)

    def log_metrics(self, run_id: str, metrics: dict[str, float]) -> LangSmithOperationResult:
        client = self._get_client()
        client.update_run(run_id=run_id, extra={"metrics": metrics})
        return LangSmithOperationResult(success=True, operation="log_metrics", run_id=run_id)

    def log_feedback(self, run_id: str, feedback_key: str, score: float | None = None, comment: str = "") -> LangSmithOperationResult:
        client = self._get_client()
        feedback = client.create_feedback(run_id=run_id, key=feedback_key, score=score, comment=comment)
        return LangSmithOperationResult(success=True, operation="log_feedback", run_id=run_id, feedback_id=str(getattr(feedback, "id", "")))

    def dataset_upload(self, dataset_name: str, examples: list[dict[str, Any]]) -> LangSmithOperationResult:
        client = self._get_client()
        try:
            dataset = client.read_dataset(dataset_name=dataset_name)
        except Exception:
            dataset = client.create_dataset(dataset_name=dataset_name)
        for example in examples:
            client.create_example(inputs=example.get("inputs", {}), outputs=example.get("outputs", {}), dataset_id=dataset.id)
        return LangSmithOperationResult(success=True, operation="dataset_upload", dataset_id=str(dataset.id))


class FakeLangSmithProvider:
    """In-memory LangSmithProvider - deterministic, no real LangSmith account
    needed. Requires a trace to exist before a run can be attached to it, and a run
    to exist before metrics/feedback can be logged against it, mirroring LangSmith's
    real run-tree model, so the lifecycle test exercises genuine state transitions
    rather than canned responses. Also enforces that nothing can be added to a
    trace that has already been ended.
    """

    name = "fake_langsmith"

    def __init__(self) -> None:
        self._traces: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._datasets: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def health_check(self) -> LangSmithHealthStatus:
        return LangSmithHealthStatus(healthy=True, configured=True, project="fake-project")

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def _get_trace(self, trace_id: str) -> dict[str, Any]:
        if trace_id not in self._traces:
            raise ValueError(f"trace '{trace_id}' does not exist")
        return self._traces[trace_id]

    def _get_run(self, run_id: str) -> dict[str, Any]:
        if run_id not in self._runs:
            raise ValueError(f"run '{run_id}' does not exist")
        return self._runs[run_id]

    def create_trace(self, trace_name: str, inputs: dict[str, Any] | None = None) -> LangSmithOperationResult:
        trace_id = self._next_id("trace")
        self._traces[trace_id] = {"name": trace_name, "inputs": inputs or {}, "metadata": {}, "outputs": None, "ended": False, "error": None}
        return LangSmithOperationResult(success=True, operation="create_trace", trace_id=trace_id)

    def update_trace(self, trace_id: str, metadata: dict[str, Any] | None = None) -> LangSmithOperationResult:
        trace = self._get_trace(trace_id)
        if trace["ended"]:
            raise RuntimeError(f"trace '{trace_id}' has already ended - cannot update it")
        trace["metadata"].update(metadata or {})
        return LangSmithOperationResult(success=True, operation="update_trace", trace_id=trace_id)

    def end_trace(self, trace_id: str, outputs: dict[str, Any] | None = None, error: str | None = None) -> LangSmithOperationResult:
        trace = self._get_trace(trace_id)
        if trace["ended"]:
            raise RuntimeError(f"trace '{trace_id}' has already ended")
        trace["ended"] = True
        trace["outputs"] = outputs or {}
        trace["error"] = error
        return LangSmithOperationResult(success=True, operation="end_trace", trace_id=trace_id)

    def create_run(self, trace_id: str, run_name: str, run_type: str = "chain", inputs: dict[str, Any] | None = None) -> LangSmithOperationResult:
        trace = self._get_trace(trace_id)
        if trace["ended"]:
            raise RuntimeError(f"cannot add a run to trace '{trace_id}' - it has already ended")
        run_id = self._next_id("run")
        self._runs[run_id] = {"trace_id": trace_id, "name": run_name, "run_type": run_type, "inputs": inputs or {}, "metrics": {}, "feedback": []}
        return LangSmithOperationResult(success=True, operation="create_run", run_id=run_id, trace_id=trace_id)

    def log_metrics(self, run_id: str, metrics: dict[str, float]) -> LangSmithOperationResult:
        run = self._get_run(run_id)
        run["metrics"].update(metrics)
        return LangSmithOperationResult(success=True, operation="log_metrics", run_id=run_id)

    def log_feedback(self, run_id: str, feedback_key: str, score: float | None = None, comment: str = "") -> LangSmithOperationResult:
        run = self._get_run(run_id)
        feedback_id = self._next_id("feedback")
        run["feedback"].append({"id": feedback_id, "key": feedback_key, "score": score, "comment": comment})
        return LangSmithOperationResult(success=True, operation="log_feedback", run_id=run_id, feedback_id=feedback_id)

    def dataset_upload(self, dataset_name: str, examples: list[dict[str, Any]]) -> LangSmithOperationResult:
        existing = next((did for did, d in self._datasets.items() if d["name"] == dataset_name), None)
        if existing is None:
            dataset_id = self._next_id("dataset")
            self._datasets[dataset_id] = {"name": dataset_name, "examples": []}
        else:
            dataset_id = existing
        self._datasets[dataset_id]["examples"].extend(examples)
        return LangSmithOperationResult(success=True, operation="dataset_upload", dataset_id=dataset_id)


_provider: LangSmithProvider = LangSmithSDKProvider()


def get_langsmith_provider() -> LangSmithProvider:
    return _provider


def set_langsmith_provider(provider: LangSmithProvider) -> None:
    """Swappable, not cached - lets tests inject a deterministic fake provider."""
    global _provider
    _provider = provider
