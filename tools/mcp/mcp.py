from typing import Any

from pydantic import BaseModel

from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.mcp.mcp_providers import get_mcp_provider

_RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_seconds=1.0)
_TIMEOUT_SECONDS = 20.0

# Field names deliberately never "name" - see the docstring in mcp_providers.py for
# why.


class HealthCheckArgs(BaseModel):
    server_name: str | None = None


class ListServersArgs(BaseModel):
    pass


class ConnectServerArgs(BaseModel):
    server_name: str


class DisconnectServerArgs(BaseModel):
    session_id: str


class ListToolsArgs(BaseModel):
    session_id: str


class CallToolArgs(BaseModel):
    session_id: str
    tool_name: str
    arguments: dict[str, Any] | None = None


class ReadResourceArgs(BaseModel):
    session_id: str
    resource_uri: str


class ExecutePromptArgs(BaseModel):
    session_id: str
    prompt_name: str
    arguments: dict[str, Any] | None = None


def mcp_health_check(server_name: str | None = None) -> dict:
    return get_mcp_provider().health_check(server_name).model_dump()


def mcp_list_servers() -> dict:
    return get_mcp_provider().list_servers().model_dump()


def mcp_connect_server(server_name: str) -> dict:
    return get_mcp_provider().connect_server(server_name).model_dump()


def mcp_disconnect_server(session_id: str) -> dict:
    return get_mcp_provider().disconnect_server(session_id).model_dump()


def mcp_list_tools(session_id: str) -> dict:
    return get_mcp_provider().list_tools(session_id).model_dump()


def mcp_call_tool(session_id: str, tool_name: str, arguments: dict[str, Any] | None = None) -> dict:
    return get_mcp_provider().call_tool(session_id, tool_name, arguments).model_dump()


def mcp_read_resource(session_id: str, resource_uri: str) -> dict:
    return get_mcp_provider().read_resource(session_id, resource_uri).model_dump()


def mcp_execute_prompt(session_id: str, prompt_name: str, arguments: dict[str, Any] | None = None) -> dict:
    return get_mcp_provider().execute_prompt(session_id, prompt_name, arguments).model_dump()


_TOOLS: list[tuple[str, str, type[BaseModel], object]] = [
    ("mcp_health_check", "Check whether the MCP subsystem (and optionally a specific server) is reachable", HealthCheckArgs, mcp_health_check),
    ("mcp_list_servers", "List configured MCP servers", ListServersArgs, mcp_list_servers),
    ("mcp_connect_server", "Connect to an MCP server, returning a session_id", ConnectServerArgs, mcp_connect_server),
    ("mcp_disconnect_server", "Disconnect an MCP server session", DisconnectServerArgs, mcp_disconnect_server),
    ("mcp_list_tools", "List tools available on a connected MCP server session", ListToolsArgs, mcp_list_tools),
    ("mcp_call_tool", "Call a tool on a connected MCP server session", CallToolArgs, mcp_call_tool),
    ("mcp_read_resource", "Read a resource from a connected MCP server session", ReadResourceArgs, mcp_read_resource),
    ("mcp_execute_prompt", "Execute (fetch) a prompt template from a connected MCP server session", ExecutePromptArgs, mcp_execute_prompt),
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
