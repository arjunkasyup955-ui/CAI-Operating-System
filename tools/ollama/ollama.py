from pydantic import BaseModel

from core.model_router import get_model_router
from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.ollama.ollama_providers import get_ollama_ops_provider

_RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_seconds=1.0)


class OllamaHealthCheckArgs(BaseModel):
    pass


class OllamaListModelsArgs(BaseModel):
    pass


class OllamaChatArgs(BaseModel):
    messages: list[dict]
    capability: str = "cheap-fast"  # routing.yaml's cheap-fast chain tries ollama first


class OllamaStreamChatArgs(BaseModel):
    messages: list[dict]
    model: str = "qwen3:8b"
    timeout_seconds: float = 60.0


def ollama_health_check() -> dict:
    return get_ollama_ops_provider().health_check().model_dump()


def ollama_list_models() -> dict:
    models = get_ollama_ops_provider().list_models()
    return {"models": [m.model_dump() for m in models], "count": len(models)}


def ollama_chat(messages: list[dict], capability: str = "cheap-fast") -> dict:
    """Delegates to the existing, frozen Model Router - never bypasses it, never
    reimplements chat completion. The Model Router's own routing.yaml already tries
    Ollama first for the 'cheap-fast' capability; this tool doesn't hardcode a
    provider, it just calls the kernel exactly as every other agent does.
    """
    response = get_model_router().chat(messages, capability=capability, agent_name="ollama_ops_agent")
    return response.model_dump()


def ollama_stream_chat(messages: list[dict], model: str = "qwen3:8b", timeout_seconds: float = 60.0) -> dict:
    chunks = list(get_ollama_ops_provider().stream_chat(messages, model, timeout_seconds=timeout_seconds))
    full_content = "".join(c.content for c in chunks)
    return {"content": full_content, "chunk_count": len(chunks), "chunks": [c.model_dump() for c in chunks]}


_TOOLS: list[tuple[str, str, type[BaseModel], object]] = [
    ("ollama_health_check", "Check whether the local Ollama service is reachable", OllamaHealthCheckArgs, ollama_health_check),
    ("ollama_list_models", "List models currently installed in the local Ollama service", OllamaListModelsArgs, ollama_list_models),
    ("ollama_chat", "Chat completion via the existing Model Router (does not bypass it)", OllamaChatArgs, ollama_chat),
    ("ollama_stream_chat", "Streaming chat completion directly against Ollama", OllamaStreamChatArgs, ollama_stream_chat),
]

for _name, _description, _schema, _func in _TOOLS:
    get_tool_registry().register(
        ToolSpec(
            name=_name,
            description=_description,
            input_schema=_schema,
            permissions=["internet_access"],
            retry_policy=_RETRY_POLICY,
            timeout_seconds=70.0,
            cost_per_call_usd=0.0,
        ),
        _func,
    )
