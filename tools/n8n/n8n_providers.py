import os
from typing import Any, Protocol

from dotenv import load_dotenv
from pydantic import BaseModel

# Deliberately self-contained (own env reading, not core.config.Settings) so this tool
# module never needs the frozen Phase 0 kernel to change to gain a new config key -
# same convention as tools/web, tools/ollama, tools/database, tools/chroma, tools/docker.
load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


class N8nHealthStatus(BaseModel):
    healthy: bool
    base_url: str
    error: str = ""


class N8nWorkflow(BaseModel):
    id: str
    workflow_name: str = ""
    active: bool = False
    nodes: list[dict[str, Any]] = []
    connections: dict[str, Any] = {}


class N8nExecution(BaseModel):
    id: str
    workflow_id: str = ""
    status: str = ""
    started_at: str = ""
    finished_at: str = ""


class N8nOperationResult(BaseModel):
    success: bool
    operation: str
    workflows: list[N8nWorkflow] = []
    executions: list[N8nExecution] = []
    workflow_id: str = ""
    error: str = ""


class N8nProvider(Protocol):
    """Workflow lifecycle management. Every method either returns an *Result on
    success or raises on failure - the agent layer's try/except (with ToolRegistry's
    RetryPolicy) handles graceful degradation uniformly, the same pattern as every
    prior provider. Field names deliberately avoid "name" anywhere (ToolRegistry.
    invoke()'s own positional parameter is literally called `name` - a schema field
    called `name` collides with it; found and fixed in the Chroma component, Phase 3
    Component 3, avoided proactively here via "workflow_name").
    """

    name: str

    def health_check(self) -> N8nHealthStatus: ...
    def list_workflows(self) -> N8nOperationResult: ...
    def get_workflow(self, workflow_id: str) -> N8nOperationResult: ...
    def create_workflow(self, workflow_name: str, nodes: list[dict[str, Any]], connections: dict[str, Any]) -> N8nOperationResult: ...
    def update_workflow(self, workflow_id: str, nodes: list[dict[str, Any]] | None = None, connections: dict[str, Any] | None = None) -> N8nOperationResult: ...
    def activate_workflow(self, workflow_id: str) -> N8nOperationResult: ...
    def deactivate_workflow(self, workflow_id: str) -> N8nOperationResult: ...
    def delete_workflow(self, workflow_id: str) -> N8nOperationResult: ...
    def execute_workflow(self, workflow_id: str, input_data: dict[str, Any] | None = None) -> N8nOperationResult: ...
    def workflow_executions(self, workflow_id: str, limit: int = 20) -> N8nOperationResult: ...


class N8nRESTProvider:
    """Default N8nProvider - real integration via n8n's REST API v1, over httpx
    (already an AFOS dependency - no new package needed, unlike the SDK-based
    providers for Postgres/Chroma/Docker). The HTTP client itself is lazily
    initialized on first use ("lazy initialization"), and every call gracefully
    surfaces connection failures rather than crashing, exactly the same defensive
    posture as every other real provider in this project.
    """

    name = "n8n_rest"

    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        self._base_url = base_url or _env("N8N_BASE_URL", "http://localhost:5678")
        self._api_key = api_key or _env("N8N_API_KEY", "")
        self._client = None

    def _get_client(self):
        if self._client is None:
            import httpx

            headers = {"X-N8N-API-KEY": self._api_key} if self._api_key else {}
            self._client = httpx.Client(base_url=self._base_url, headers=headers, timeout=10.0)
        return self._client

    def health_check(self) -> N8nHealthStatus:
        try:
            response = self._get_client().get("/healthz", timeout=5.0)
            response.raise_for_status()
            return N8nHealthStatus(healthy=True, base_url=self._base_url)
        except Exception as exc:
            return N8nHealthStatus(healthy=False, base_url=self._base_url, error=str(exc))

    def list_workflows(self) -> N8nOperationResult:
        response = self._get_client().get("/api/v1/workflows")
        response.raise_for_status()
        data = response.json()
        workflows = [
            N8nWorkflow(id=str(w.get("id")), workflow_name=w.get("name", ""), active=w.get("active", False), nodes=w.get("nodes", []), connections=w.get("connections", {}))
            for w in data.get("data", [])
        ]
        return N8nOperationResult(success=True, operation="list_workflows", workflows=workflows)

    def get_workflow(self, workflow_id: str) -> N8nOperationResult:
        response = self._get_client().get(f"/api/v1/workflows/{workflow_id}")
        response.raise_for_status()
        w = response.json()
        workflow = N8nWorkflow(id=str(w.get("id")), workflow_name=w.get("name", ""), active=w.get("active", False), nodes=w.get("nodes", []), connections=w.get("connections", {}))
        return N8nOperationResult(success=True, operation="get_workflow", workflows=[workflow], workflow_id=workflow_id)

    def create_workflow(self, workflow_name: str, nodes: list[dict[str, Any]], connections: dict[str, Any]) -> N8nOperationResult:
        response = self._get_client().post("/api/v1/workflows", json={"name": workflow_name, "nodes": nodes, "connections": connections})
        response.raise_for_status()
        w = response.json()
        return N8nOperationResult(success=True, operation="create_workflow", workflow_id=str(w.get("id")))

    def update_workflow(self, workflow_id: str, nodes: list[dict[str, Any]] | None = None, connections: dict[str, Any] | None = None) -> N8nOperationResult:
        body: dict[str, Any] = {}
        if nodes is not None:
            body["nodes"] = nodes
        if connections is not None:
            body["connections"] = connections
        response = self._get_client().put(f"/api/v1/workflows/{workflow_id}", json=body)
        response.raise_for_status()
        return N8nOperationResult(success=True, operation="update_workflow", workflow_id=workflow_id)

    def activate_workflow(self, workflow_id: str) -> N8nOperationResult:
        response = self._get_client().post(f"/api/v1/workflows/{workflow_id}/activate")
        response.raise_for_status()
        return N8nOperationResult(success=True, operation="activate_workflow", workflow_id=workflow_id)

    def deactivate_workflow(self, workflow_id: str) -> N8nOperationResult:
        response = self._get_client().post(f"/api/v1/workflows/{workflow_id}/deactivate")
        response.raise_for_status()
        return N8nOperationResult(success=True, operation="deactivate_workflow", workflow_id=workflow_id)

    def delete_workflow(self, workflow_id: str) -> N8nOperationResult:
        response = self._get_client().delete(f"/api/v1/workflows/{workflow_id}")
        response.raise_for_status()
        return N8nOperationResult(success=True, operation="delete_workflow", workflow_id=workflow_id)

    def execute_workflow(self, workflow_id: str, input_data: dict[str, Any] | None = None) -> N8nOperationResult:
        # NOTE: n8n's public REST API has not historically guaranteed a stable,
        # version-independent "run by id" endpoint - this targets the commonly
        # documented /workflows/{id}/run pattern. Unverified against a live server in
        # this sandbox (none is running); adjust the path if your n8n version differs.
        response = self._get_client().post(f"/api/v1/workflows/{workflow_id}/run", json={"workflowData": input_data or {}})
        response.raise_for_status()
        data = response.json()
        execution = N8nExecution(id=str(data.get("executionId", "")), workflow_id=workflow_id, status=data.get("status", ""))
        return N8nOperationResult(success=True, operation="execute_workflow", workflow_id=workflow_id, executions=[execution])

    def workflow_executions(self, workflow_id: str, limit: int = 20) -> N8nOperationResult:
        response = self._get_client().get("/api/v1/executions", params={"workflowId": workflow_id, "limit": limit})
        response.raise_for_status()
        data = response.json()
        executions = [
            N8nExecution(id=str(e.get("id")), workflow_id=workflow_id, status=e.get("status", ""), started_at=e.get("startedAt", ""), finished_at=e.get("stoppedAt", ""))
            for e in data.get("data", [])
        ]
        return N8nOperationResult(success=True, operation="workflow_executions", executions=executions, workflow_id=workflow_id)


class FakeN8nProvider:
    """In-memory N8nProvider - deterministic, no real n8n instance needed. Requires a
    workflow to be activated before it can be executed, mirroring n8n's real
    semantics, so the lifecycle test exercises genuine state transitions rather than
    canned responses.
    """

    name = "fake_n8n"

    def __init__(self) -> None:
        self._workflows: dict[str, dict[str, Any]] = {}
        self._executions: dict[str, list[dict[str, Any]]] = {}
        self._counter = 0

    def health_check(self) -> N8nHealthStatus:
        return N8nHealthStatus(healthy=True, base_url="fake")

    def list_workflows(self) -> N8nOperationResult:
        workflows = [
            N8nWorkflow(id=wid, workflow_name=w["workflow_name"], active=w["active"], nodes=w["nodes"], connections=w["connections"])
            for wid, w in self._workflows.items()
        ]
        return N8nOperationResult(success=True, operation="list_workflows", workflows=workflows)

    def _get(self, workflow_id: str) -> dict[str, Any]:
        if workflow_id not in self._workflows:
            raise ValueError(f"workflow '{workflow_id}' does not exist")
        return self._workflows[workflow_id]

    def get_workflow(self, workflow_id: str) -> N8nOperationResult:
        w = self._get(workflow_id)
        workflow = N8nWorkflow(id=workflow_id, workflow_name=w["workflow_name"], active=w["active"], nodes=w["nodes"], connections=w["connections"])
        return N8nOperationResult(success=True, operation="get_workflow", workflows=[workflow], workflow_id=workflow_id)

    def create_workflow(self, workflow_name: str, nodes: list[dict[str, Any]], connections: dict[str, Any]) -> N8nOperationResult:
        self._counter += 1
        workflow_id = f"wf-{self._counter}"
        self._workflows[workflow_id] = {"workflow_name": workflow_name, "nodes": nodes, "connections": connections, "active": False}
        self._executions[workflow_id] = []
        return N8nOperationResult(success=True, operation="create_workflow", workflow_id=workflow_id)

    def update_workflow(self, workflow_id: str, nodes: list[dict[str, Any]] | None = None, connections: dict[str, Any] | None = None) -> N8nOperationResult:
        w = self._get(workflow_id)
        if nodes is not None:
            w["nodes"] = nodes
        if connections is not None:
            w["connections"] = connections
        return N8nOperationResult(success=True, operation="update_workflow", workflow_id=workflow_id)

    def activate_workflow(self, workflow_id: str) -> N8nOperationResult:
        self._get(workflow_id)["active"] = True
        return N8nOperationResult(success=True, operation="activate_workflow", workflow_id=workflow_id)

    def deactivate_workflow(self, workflow_id: str) -> N8nOperationResult:
        self._get(workflow_id)["active"] = False
        return N8nOperationResult(success=True, operation="deactivate_workflow", workflow_id=workflow_id)

    def delete_workflow(self, workflow_id: str) -> N8nOperationResult:
        self._get(workflow_id)
        del self._workflows[workflow_id]
        self._executions.pop(workflow_id, None)
        return N8nOperationResult(success=True, operation="delete_workflow", workflow_id=workflow_id)

    def execute_workflow(self, workflow_id: str, input_data: dict[str, Any] | None = None) -> N8nOperationResult:
        w = self._get(workflow_id)
        if not w["active"]:
            raise RuntimeError(f"workflow '{workflow_id}' is not active - activate it before executing")
        exec_id = f"exec-{len(self._executions[workflow_id]) + 1}"
        self._executions[workflow_id].append({"id": exec_id, "status": "success"})
        return N8nOperationResult(
            success=True, operation="execute_workflow", workflow_id=workflow_id,
            executions=[N8nExecution(id=exec_id, workflow_id=workflow_id, status="success")],
        )

    def workflow_executions(self, workflow_id: str, limit: int = 20) -> N8nOperationResult:
        self._get(workflow_id)
        execs = self._executions.get(workflow_id, [])[-limit:]
        executions = [N8nExecution(id=e["id"], workflow_id=workflow_id, status=e["status"]) for e in execs]
        return N8nOperationResult(success=True, operation="workflow_executions", executions=executions, workflow_id=workflow_id)


_provider: N8nProvider = N8nRESTProvider()


def get_n8n_provider() -> N8nProvider:
    return _provider


def set_n8n_provider(provider: N8nProvider) -> None:
    """Swappable, not cached - lets tests inject a deterministic fake provider."""
    global _provider
    _provider = provider
