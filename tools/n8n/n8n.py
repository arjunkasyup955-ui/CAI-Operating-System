from typing import Any

from pydantic import BaseModel

from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.n8n.n8n_providers import get_n8n_provider

_RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_seconds=1.0)
_TIMEOUT_SECONDS = 20.0

# Field names deliberately never "name" - see the docstring in n8n_providers.py for why.


class HealthCheckArgs(BaseModel):
    pass


class ListWorkflowsArgs(BaseModel):
    pass


class GetWorkflowArgs(BaseModel):
    workflow_id: str


class CreateWorkflowArgs(BaseModel):
    workflow_name: str
    nodes: list[dict[str, Any]] = []
    connections: dict[str, Any] = {}


class UpdateWorkflowArgs(BaseModel):
    workflow_id: str
    nodes: list[dict[str, Any]] | None = None
    connections: dict[str, Any] | None = None


class ActivateWorkflowArgs(BaseModel):
    workflow_id: str


class DeactivateWorkflowArgs(BaseModel):
    workflow_id: str


class DeleteWorkflowArgs(BaseModel):
    workflow_id: str


class ExecuteWorkflowArgs(BaseModel):
    workflow_id: str
    input_data: dict[str, Any] | None = None


class WorkflowExecutionsArgs(BaseModel):
    workflow_id: str
    limit: int = 20


def n8n_health_check() -> dict:
    return get_n8n_provider().health_check().model_dump()


def n8n_list_workflows() -> dict:
    return get_n8n_provider().list_workflows().model_dump()


def n8n_get_workflow(workflow_id: str) -> dict:
    return get_n8n_provider().get_workflow(workflow_id).model_dump()


def n8n_create_workflow(workflow_name: str, nodes: list[dict[str, Any]] | None = None, connections: dict[str, Any] | None = None) -> dict:
    return get_n8n_provider().create_workflow(workflow_name, nodes or [], connections or {}).model_dump()


def n8n_update_workflow(workflow_id: str, nodes: list[dict[str, Any]] | None = None, connections: dict[str, Any] | None = None) -> dict:
    return get_n8n_provider().update_workflow(workflow_id, nodes, connections).model_dump()


def n8n_activate_workflow(workflow_id: str) -> dict:
    return get_n8n_provider().activate_workflow(workflow_id).model_dump()


def n8n_deactivate_workflow(workflow_id: str) -> dict:
    return get_n8n_provider().deactivate_workflow(workflow_id).model_dump()


def n8n_delete_workflow(workflow_id: str) -> dict:
    return get_n8n_provider().delete_workflow(workflow_id).model_dump()


def n8n_execute_workflow(workflow_id: str, input_data: dict[str, Any] | None = None) -> dict:
    return get_n8n_provider().execute_workflow(workflow_id, input_data).model_dump()


def n8n_workflow_executions(workflow_id: str, limit: int = 20) -> dict:
    return get_n8n_provider().workflow_executions(workflow_id, limit).model_dump()


_TOOLS: list[tuple[str, str, type[BaseModel], object]] = [
    ("n8n_health_check", "Check whether the n8n instance is reachable", HealthCheckArgs, n8n_health_check),
    ("n8n_list_workflows", "List workflows", ListWorkflowsArgs, n8n_list_workflows),
    ("n8n_get_workflow", "Get a single workflow by id", GetWorkflowArgs, n8n_get_workflow),
    ("n8n_create_workflow", "Create a workflow", CreateWorkflowArgs, n8n_create_workflow),
    ("n8n_update_workflow", "Update a workflow's nodes/connections", UpdateWorkflowArgs, n8n_update_workflow),
    ("n8n_activate_workflow", "Activate a workflow", ActivateWorkflowArgs, n8n_activate_workflow),
    ("n8n_deactivate_workflow", "Deactivate a workflow", DeactivateWorkflowArgs, n8n_deactivate_workflow),
    ("n8n_delete_workflow", "Delete a workflow", DeleteWorkflowArgs, n8n_delete_workflow),
    ("n8n_execute_workflow", "Execute a workflow", ExecuteWorkflowArgs, n8n_execute_workflow),
    ("n8n_workflow_executions", "List a workflow's past executions", WorkflowExecutionsArgs, n8n_workflow_executions),
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
