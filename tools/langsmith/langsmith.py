from typing import Any

from pydantic import BaseModel

from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.langsmith.langsmith_providers import get_langsmith_provider

_RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_seconds=1.0)
_TIMEOUT_SECONDS = 20.0

# Field names deliberately never "name" - see the docstring in
# langsmith_providers.py for why.


class HealthCheckArgs(BaseModel):
    pass


class CreateTraceArgs(BaseModel):
    trace_name: str
    inputs: dict[str, Any] | None = None


class UpdateTraceArgs(BaseModel):
    trace_id: str
    metadata: dict[str, Any] | None = None


class EndTraceArgs(BaseModel):
    trace_id: str
    outputs: dict[str, Any] | None = None
    error: str | None = None


class CreateRunArgs(BaseModel):
    trace_id: str
    run_name: str
    run_type: str = "chain"
    inputs: dict[str, Any] | None = None


class LogMetricsArgs(BaseModel):
    run_id: str
    metrics: dict[str, float]


class LogFeedbackArgs(BaseModel):
    run_id: str
    feedback_key: str
    score: float | None = None
    comment: str = ""


class DatasetUploadArgs(BaseModel):
    dataset_name: str
    examples: list[dict[str, Any]]


def langsmith_health_check() -> dict:
    return get_langsmith_provider().health_check().model_dump()


def langsmith_create_trace(trace_name: str, inputs: dict[str, Any] | None = None) -> dict:
    return get_langsmith_provider().create_trace(trace_name, inputs).model_dump()


def langsmith_update_trace(trace_id: str, metadata: dict[str, Any] | None = None) -> dict:
    return get_langsmith_provider().update_trace(trace_id, metadata).model_dump()


def langsmith_end_trace(trace_id: str, outputs: dict[str, Any] | None = None, error: str | None = None) -> dict:
    return get_langsmith_provider().end_trace(trace_id, outputs, error).model_dump()


def langsmith_create_run(trace_id: str, run_name: str, run_type: str = "chain", inputs: dict[str, Any] | None = None) -> dict:
    return get_langsmith_provider().create_run(trace_id, run_name, run_type, inputs).model_dump()


def langsmith_log_metrics(run_id: str, metrics: dict[str, float]) -> dict:
    return get_langsmith_provider().log_metrics(run_id, metrics).model_dump()


def langsmith_log_feedback(run_id: str, feedback_key: str, score: float | None = None, comment: str = "") -> dict:
    return get_langsmith_provider().log_feedback(run_id, feedback_key, score, comment).model_dump()


def langsmith_dataset_upload(dataset_name: str, examples: list[dict[str, Any]]) -> dict:
    return get_langsmith_provider().dataset_upload(dataset_name, examples).model_dump()


_TOOLS: list[tuple[str, str, type[BaseModel], object]] = [
    ("langsmith_health_check", "Check whether LangSmith is configured and reachable", HealthCheckArgs, langsmith_health_check),
    ("langsmith_create_trace", "Create a new root trace", CreateTraceArgs, langsmith_create_trace),
    ("langsmith_update_trace", "Update a trace's metadata", UpdateTraceArgs, langsmith_update_trace),
    ("langsmith_end_trace", "End a trace with outputs/error", EndTraceArgs, langsmith_end_trace),
    ("langsmith_create_run", "Create a child run under a trace", CreateRunArgs, langsmith_create_run),
    ("langsmith_log_metrics", "Log numeric metrics against a run", LogMetricsArgs, langsmith_log_metrics),
    ("langsmith_log_feedback", "Log feedback (score/comment) against a run", LogFeedbackArgs, langsmith_log_feedback),
    ("langsmith_dataset_upload", "Upload examples to a named dataset", DatasetUploadArgs, langsmith_dataset_upload),
]

for _name, _description, _schema, _func in _TOOLS:
    get_tool_registry().register(
        ToolSpec(
            name=_name,
            description=_description,
            input_schema=_schema,
            permissions=["internet_access"],
            retry_policy=_RETRY_POLICY,
            timeout_seconds=_TIMEOUT_SECONDS,
            cost_per_call_usd=0.0,
        ),
        _func,
    )
