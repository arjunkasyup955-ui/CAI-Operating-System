import asyncio
import os
from typing import Any, Protocol

from dotenv import load_dotenv
from pydantic import BaseModel

# Deliberately self-contained (own env reading, not core.config.Settings) so this
# tool module never needs the frozen Phase 0 kernel to change to gain a new config
# key - same convention as tools/n8n, tools/playwright, tools/crawl4ai, tools/searxng.
load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


class MCPHealthStatus(BaseModel):
    healthy: bool
    installed: bool
    server_name: str = ""
    error: str = ""


class MCPServerInfo(BaseModel):
    server_name: str
    connected: bool = False
    description: str = ""


class MCPToolInfo(BaseModel):
    tool_name: str
    description: str = ""
    input_schema: dict[str, Any] = {}


class MCPOperationResult(BaseModel):
    success: bool
    operation: str
    session_id: str = ""
    servers: list[MCPServerInfo] = []
    tools: list[MCPToolInfo] = []
    content: Any = None
    error: str = ""


class MCPProvider(Protocol):
    """Remote-tool-server connectivity via the Model Context Protocol. Every method
    either returns an MCPOperationResult/MCPHealthStatus on success or raises on
    failure - the agent layer's try/except (with ToolRegistry's RetryPolicy) handles
    graceful degradation uniformly, the same pattern as every prior provider. Field
    names deliberately avoid "name" anywhere (ToolRegistry.invoke()'s own positional
    parameter is literally called `name` - a schema field called `name` collides
    with it; found and fixed in the Chroma component, Phase 3 Component 3; avoided
    proactively here via "server_name"/"tool_name"/"prompt_name").
    """

    name: str

    def health_check(self, server_name: str | None = None) -> MCPHealthStatus: ...
    def list_servers(self) -> MCPOperationResult: ...
    def connect_server(self, server_name: str) -> MCPOperationResult: ...
    def disconnect_server(self, session_id: str) -> MCPOperationResult: ...
    def list_tools(self, session_id: str) -> MCPOperationResult: ...
    def call_tool(self, session_id: str, tool_name: str, arguments: dict[str, Any] | None = None) -> MCPOperationResult: ...
    def read_resource(self, session_id: str, resource_uri: str) -> MCPOperationResult: ...
    def execute_prompt(self, session_id: str, prompt_name: str, arguments: dict[str, Any] | None = None) -> MCPOperationResult: ...


class MCPSDKProvider:
    """Default MCPProvider - real integration via the official `mcp` Python SDK
    (confirmed not installed in this sandbox), lazily imported so this module loads
    fine regardless of whether it's installed. Session Management here is
    deliberately lightweight: connect_server only validates that a server's
    connection config is present (via MCP_SERVER_<NAME>_COMMAND/_ARGS, for a stdio
    subprocess server, or _URL for an SSE server) and records it under a session_id
    - it does NOT hold a live subprocess/transport open between calls. Each
    subsequent operation (list_tools/call_tool/read_resource/execute_prompt) opens a
    fresh short-lived MCP connection via asyncio.run(), performs the one action, and
    tears it down - trading persistent-connection efficiency for much simpler, more
    honestly-verifiable code, since no live MCP server exists in this sandbox to
    validate a background-thread/event-loop-based persistent session against
    anyway. If true persistent sessions are needed later, this is the seam to
    replace with a dedicated event-loop thread per session. Unverified against a
    live MCP server/SDK version - adjust the stdio_client/ClientSession call shapes
    if your installed `mcp` version's API differs.
    """

    name = "mcp_sdk"

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def _server_config(self, server_name: str) -> dict[str, Any]:
        key = server_name.upper().replace("-", "_")
        command = _env(f"MCP_SERVER_{key}_COMMAND")
        args = _env(f"MCP_SERVER_{key}_ARGS")
        url = _env(f"MCP_SERVER_{key}_URL")
        return {"command": command, "args": args.split() if args else [], "url": url}

    def health_check(self, server_name: str | None = None) -> MCPHealthStatus:
        try:
            import mcp  # noqa: F401
        except ImportError as exc:
            return MCPHealthStatus(healthy=False, installed=False, server_name=server_name or "", error=str(exc))

        if server_name is None:
            return MCPHealthStatus(healthy=True, installed=True)

        config = self._server_config(server_name)
        if not config["command"] and not config["url"]:
            return MCPHealthStatus(
                healthy=False, installed=True, server_name=server_name,
                error=f"MCP server '{server_name}' is not configured (set MCP_SERVER_{server_name.upper()}_COMMAND/_ARGS or _URL)",
            )
        try:
            asyncio.run(asyncio.wait_for(self._probe(config), timeout=10.0))
            return MCPHealthStatus(healthy=True, installed=True, server_name=server_name)
        except TimeoutError:
            return MCPHealthStatus(healthy=False, installed=True, server_name=server_name, error=f"connection to '{server_name}' timed out")
        except Exception as exc:
            return MCPHealthStatus(healthy=False, installed=True, server_name=server_name, error=str(exc))

    async def _probe(self, config: dict[str, Any]) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(command=config["command"], args=config["args"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

    def list_servers(self) -> MCPOperationResult:
        names = [n.strip() for n in _env("MCP_SERVERS").split(",") if n.strip()]
        servers = [MCPServerInfo(server_name=n, connected=False, description="configured via MCP_SERVERS env var") for n in names]
        return MCPOperationResult(success=True, operation="list_servers", servers=servers)

    def connect_server(self, server_name: str) -> MCPOperationResult:
        try:
            import mcp  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("mcp SDK is not installed - `pip install mcp`") from exc

        config = self._server_config(server_name)
        if not config["command"] and not config["url"]:
            raise RuntimeError(f"MCP server '{server_name}' is not configured (set MCP_SERVER_{server_name.upper()}_COMMAND/_ARGS or _URL)")

        self._counter += 1
        session_id = f"session-{self._counter}"
        self._sessions[session_id] = {"server_name": server_name, **config}
        return MCPOperationResult(success=True, operation="connect_server", session_id=session_id)

    def _get_session(self, session_id: str) -> dict[str, Any]:
        if session_id not in self._sessions:
            raise ValueError(f"MCP session '{session_id}' does not exist")
        return self._sessions[session_id]

    def disconnect_server(self, session_id: str) -> MCPOperationResult:
        self._get_session(session_id)
        del self._sessions[session_id]
        return MCPOperationResult(success=True, operation="disconnect_server", session_id=session_id)

    async def _with_session(self, config: dict[str, Any], action):
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(command=config["command"], args=config["args"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await action(session)

    def _run(self, session_id: str, action, timeout: float = 15.0):
        config = self._get_session(session_id)
        try:
            return asyncio.run(asyncio.wait_for(self._with_session(config, action), timeout=timeout))
        except TimeoutError as exc:
            raise RuntimeError(f"MCP call to '{config['server_name']}' timed out after {timeout}s") from exc
        except (ConnectionError, OSError) as exc:
            raise RuntimeError(f"connection refused by MCP server '{config['server_name']}'") from exc

    def list_tools(self, session_id: str) -> MCPOperationResult:
        result = self._run(session_id, lambda s: s.list_tools())
        tools = [MCPToolInfo(tool_name=t.name, description=t.description or "", input_schema=t.inputSchema or {}) for t in result.tools]
        return MCPOperationResult(success=True, operation="list_tools", tools=tools)

    def call_tool(self, session_id: str, tool_name: str, arguments: dict[str, Any] | None = None) -> MCPOperationResult:
        result = self._run(session_id, lambda s: s.call_tool(tool_name, arguments or {}))
        if getattr(result, "isError", False):
            raise RuntimeError(f"MCP tool '{tool_name}' returned an error: {result.content}")
        return MCPOperationResult(success=True, operation="call_tool", content=[c.model_dump() for c in result.content])

    def read_resource(self, session_id: str, resource_uri: str) -> MCPOperationResult:
        result = self._run(session_id, lambda s: s.read_resource(resource_uri))
        return MCPOperationResult(success=True, operation="read_resource", content=[c.model_dump() for c in result.contents])

    def execute_prompt(self, session_id: str, prompt_name: str, arguments: dict[str, Any] | None = None) -> MCPOperationResult:
        result = self._run(session_id, lambda s: s.get_prompt(prompt_name, arguments or {}))
        return MCPOperationResult(success=True, operation="execute_prompt", content=[m.model_dump() for m in result.messages])


class FakeMCPProvider:
    """In-memory MCPProvider - deterministic, no real MCP server needed. Requires a
    server to be connected (session created) before any tool/resource/prompt
    operation, mirroring real MCP semantics, so the lifecycle test exercises
    genuine state transitions rather than canned responses. Also deterministically
    simulates all 5 graceful-failure scenarios this component must handle:
    "unreachable-server" simulates connection refused, an unconfigured server name
    simulates a server being unavailable, an unknown tool_name/resource_uri/
    prompt_name simulates an invalid-name error, and the reserved
    "malformed_tool"/"malformed://resource" identifiers simulate a malformed
    response from the server. Timeout is exercised the same way as every prior
    Phase 3 component - via ToolRegistry's own timeout mechanism on a deliberately
    slow function, not a provider-level simulation.
    """

    name = "fake_mcp"

    _KNOWN_SERVERS = {
        "docs-server": "Fake documentation server",
        "unreachable-server": "Simulates a server that refuses connections",
    }

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._counter = 0

    def health_check(self, server_name: str | None = None) -> MCPHealthStatus:
        if server_name is None:
            return MCPHealthStatus(healthy=True, installed=True)
        if server_name == "unreachable-server":
            return MCPHealthStatus(healthy=False, installed=True, server_name=server_name, error=f"connection refused by MCP server '{server_name}'")
        if server_name not in self._KNOWN_SERVERS:
            return MCPHealthStatus(healthy=False, installed=True, server_name=server_name, error=f"MCP server '{server_name}' is not configured/available")
        return MCPHealthStatus(healthy=True, installed=True, server_name=server_name)

    def list_servers(self) -> MCPOperationResult:
        servers = [
            MCPServerInfo(
                server_name=n,
                connected=any(s["server_name"] == n for s in self._sessions.values()),
                description=d,
            )
            for n, d in self._KNOWN_SERVERS.items()
        ]
        return MCPOperationResult(success=True, operation="list_servers", servers=servers)

    def _next_id(self) -> str:
        self._counter += 1
        return f"session-{self._counter}"

    def connect_server(self, server_name: str) -> MCPOperationResult:
        if server_name == "unreachable-server":
            raise ConnectionError(f"connection refused by MCP server '{server_name}'")
        if server_name not in self._KNOWN_SERVERS:
            raise ValueError(f"MCP server '{server_name}' is not configured/available")

        session_id = self._next_id()
        self._sessions[session_id] = {
            "server_name": server_name,
            "tools": {
                "search_docs": {"description": "Search the fake docs", "input_schema": {"query": "string"}},
                "malformed_tool": {"description": "Deliberately returns a malformed response", "input_schema": {}},
            },
            "resources": {"docs://readme": "# Fake README\nThis is fake resource content."},
            "prompts": {"summarize": {"description": "Summarize the given text"}},
        }
        return MCPOperationResult(success=True, operation="connect_server", session_id=session_id)

    def _get_session(self, session_id: str) -> dict[str, Any]:
        if session_id not in self._sessions:
            raise ValueError(f"MCP session '{session_id}' does not exist")
        return self._sessions[session_id]

    def disconnect_server(self, session_id: str) -> MCPOperationResult:
        self._get_session(session_id)
        del self._sessions[session_id]
        return MCPOperationResult(success=True, operation="disconnect_server", session_id=session_id)

    def list_tools(self, session_id: str) -> MCPOperationResult:
        session = self._get_session(session_id)
        tools = [MCPToolInfo(tool_name=n, description=t["description"], input_schema=t["input_schema"]) for n, t in session["tools"].items()]
        return MCPOperationResult(success=True, operation="list_tools", tools=tools)

    def call_tool(self, session_id: str, tool_name: str, arguments: dict[str, Any] | None = None) -> MCPOperationResult:
        session = self._get_session(session_id)
        if tool_name not in session["tools"]:
            raise ValueError(f"tool '{tool_name}' is not available on MCP server '{session['server_name']}'")
        if tool_name == "malformed_tool":
            raise RuntimeError("received a malformed response from the MCP server")
        return MCPOperationResult(
            success=True, operation="call_tool",
            content=[{"type": "text", "text": f"executed '{tool_name}' with arguments {arguments or {}}"}],
        )

    def read_resource(self, session_id: str, resource_uri: str) -> MCPOperationResult:
        session = self._get_session(session_id)
        if resource_uri == "malformed://resource":
            raise RuntimeError("received a malformed response from the MCP server")
        if resource_uri not in session["resources"]:
            raise ValueError(f"resource '{resource_uri}' not found on MCP server '{session['server_name']}'")
        return MCPOperationResult(success=True, operation="read_resource", content=[{"type": "text", "text": session["resources"][resource_uri]}])

    def execute_prompt(self, session_id: str, prompt_name: str, arguments: dict[str, Any] | None = None) -> MCPOperationResult:
        session = self._get_session(session_id)
        if prompt_name not in session["prompts"]:
            raise ValueError(f"prompt '{prompt_name}' not found on MCP server '{session['server_name']}'")
        return MCPOperationResult(
            success=True, operation="execute_prompt",
            content=[{"role": "user", "text": f"prompt '{prompt_name}' executed with arguments {arguments or {}}"}],
        )


_provider: MCPProvider = MCPSDKProvider()


def get_mcp_provider() -> MCPProvider:
    return _provider


def set_mcp_provider(provider: MCPProvider) -> None:
    """Swappable, not cached - lets tests inject a deterministic fake provider."""
    global _provider
    _provider = provider
