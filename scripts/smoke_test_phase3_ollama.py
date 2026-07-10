"""Phase 3, Component 1: Ollama Integration.

Verifies:
  - Provider registration (agent + 4 tools)
  - Health check (real + fake provider)
  - Installed model listing (real + fake provider)
  - Successful chat request (delegates to the existing, frozen Model Router - never
    bypasses or duplicates it)
  - Streaming (real Ollama NDJSON stream + fake provider)
  - Timeout (real network timeout against an unreachable host)
  - Retry (ToolRegistry's existing RetryPolicy, via a flaky fake provider)
  - Graceful failure (never crashes)
  - Dependency Injection using a fake provider
  - Schema validation
  - Permission enforcement
  - Event publishing (ollama_started/completed/failed)

Run: python scripts/smoke_test_phase3_ollama.py
"""

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from pydantic import ValidationError  # noqa: E402

import agents.llm.ollama.agent as ollama_agent  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import get_agent_registry, get_tool_registry  # noqa: E402
from tools.ollama.ollama_providers import (  # noqa: E402
    OllamaChatChunk,
    OllamaHealthStatus,
    OllamaModelInfo,
    RealOllamaOpsProvider,
    set_ollama_ops_provider,
)


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


class _FakeProvider:
    name = "fake_ollama"

    def health_check(self) -> OllamaHealthStatus:
        return OllamaHealthStatus(healthy=True, base_url="http://fake", version="9.9.9")

    def list_models(self) -> list[OllamaModelInfo]:
        return [OllamaModelInfo(name="fake-model", size_bytes=123, modified_at="now")]

    def stream_chat(self, messages, model, timeout_seconds=60.0):
        for chunk in ("Hello", " ", "world", ""):
            yield OllamaChatChunk(content=chunk, done=(chunk == ""))


class _FlakyProvider:
    name = "flaky"

    def __init__(self, fail_times: int = 1) -> None:
        self.calls = 0
        self._fail_times = fail_times

    def health_check(self) -> OllamaHealthStatus:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("transient failure")
        return OllamaHealthStatus(healthy=True, base_url="http://fake", version="ok")

    def list_models(self):
        return []

    def stream_chat(self, messages, model, timeout_seconds=60.0):
        yield OllamaChatChunk(content="", done=True)


class _AlwaysFailingProvider:
    name = "always_failing"

    def health_check(self):
        raise RuntimeError("permanent failure")

    def list_models(self):
        raise RuntimeError("permanent failure")

    def stream_chat(self, messages, model, timeout_seconds=60.0):
        raise RuntimeError("permanent failure")
        yield  # pragma: no cover - unreachable, makes this a generator


def main() -> None:
    print("\n== 1. Provider Registration (agent + tools) ==")
    manifest = get_agent_registry().get("ollama_ops_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares internet_access permission", manifest.permissions == ["internet_access"])
    check(
        "declares all 4 tools",
        set(manifest.tools) == {"ollama_health_check", "ollama_list_models", "ollama_chat", "ollama_stream_chat"},
    )
    check("lifecycle is active", manifest.lifecycle == "active")
    for tool_name in ("ollama_health_check", "ollama_list_models", "ollama_chat", "ollama_stream_chat"):
        spec = get_tool_registry().get_spec(tool_name)
        check(f"{tool_name} requires internet_access permission", "internet_access" in spec.permissions)

    print("\n== 2. Schema Validation ==")
    try:
        get_tool_registry().invoke("ollama_chat", agent_name="ollama_ops_agent")
        rejected = False
    except ValidationError:
        rejected = True
    check("missing required 'messages' field rejected before execution", rejected)

    print("\n== 3. Permission Enforcement ==")
    manifest_no_perms = manifest.model_copy(update={"name": "no_net_ollama_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("ollama_health_check", agent_name="no_net_ollama_agent")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without internet_access is denied", denied)

    print("\n== 4. Health Check (real local Ollama) ==")
    real_health = ollama_agent.run_ollama_operation("ollama_health_check", venture_id="v-test")
    check("real health check completed", real_health["event"] == "ollama_completed")
    check("local Ollama reports healthy", real_health["healthy"] is True)
    print(f"     real Ollama version: {real_health.get('version')}")

    print("\n== 5. Installed Model Listing (real local Ollama) ==")
    real_models = ollama_agent.run_ollama_operation("ollama_list_models", venture_id="v-test")
    check("real model listing completed", real_models["event"] == "ollama_completed")
    check("at least one model installed", real_models["count"] >= 1)
    print(f"     installed models: {[m['name'] for m in real_models['models']]}")

    print("\n== 6. Dependency Injection (fake provider for health/models/stream) ==")
    set_ollama_ops_provider(_FakeProvider())
    try:
        fake_health = ollama_agent.run_ollama_operation("ollama_health_check", venture_id="v-test")
        check("fake provider health check returns injected values", fake_health["version"] == "9.9.9")

        fake_models = ollama_agent.run_ollama_operation("ollama_list_models", venture_id="v-test")
        check("fake provider model listing returns injected values", fake_models["models"][0]["name"] == "fake-model")

        fake_stream = ollama_agent.run_ollama_operation(
            "ollama_stream_chat", venture_id="v-test", messages=[{"role": "user", "content": "hi"}], model="fake-model"
        )
        check("fake provider streaming concatenates chunks correctly", fake_stream["content"] == "Hello world")
        check("fake provider streaming reports the right chunk count", fake_stream["chunk_count"] == 4)
    finally:
        set_ollama_ops_provider(RealOllamaOpsProvider())

    print("\n== 7. Streaming (real local Ollama) ==")
    real_stream = ollama_agent.run_ollama_operation(
        "ollama_stream_chat", venture_id="v-test", messages=[{"role": "user", "content": "Reply with exactly: hi"}], model="qwen3:8b"
    )
    check("real streaming completed", real_stream["event"] == "ollama_completed")
    check("real streaming produced at least one chunk", real_stream["chunk_count"] >= 1)
    check("real streaming produced non-empty content", len(real_stream["content"]) > 0)

    print("\n== 8. Timeout (real network timeout against an unreachable host) ==")
    unreachable_provider = RealOllamaOpsProvider(base_url="http://10.255.255.1:11434")
    start = time.monotonic()
    timeout_result = unreachable_provider.health_check()
    elapsed = time.monotonic() - start
    check("unreachable host reported as unhealthy, not raised", timeout_result.healthy is False)
    check("timeout error captured", bool(timeout_result.error))
    check("respected the provider's own timeout (didn't hang)", elapsed < 10.0)

    print("\n== 9. Retry Behaviour (ToolRegistry's existing RetryPolicy) ==")
    flaky = _FlakyProvider(fail_times=1)
    set_ollama_ops_provider(flaky)
    try:
        retry_entry = ollama_agent.run_ollama_operation("ollama_health_check", venture_id="v-test")
        check("retried past one transient failure and completed", retry_entry["event"] == "ollama_completed")
        check("exactly 2 attempts were made", flaky.calls == 2)
    finally:
        set_ollama_ops_provider(RealOllamaOpsProvider())

    print("\n== 10. Graceful Failure Handling ==")
    set_ollama_ops_provider(_AlwaysFailingProvider())
    try:
        fail_entry = ollama_agent.run_ollama_operation("ollama_health_check", venture_id="v-test")
        check("exhausted retries return ollama_failed, never raises", fail_entry["event"] == "ollama_failed")
    finally:
        set_ollama_ops_provider(RealOllamaOpsProvider())

    print("\n== 11. Successful Chat Request (delegates to the existing Model Router) ==")
    chat_entry = ollama_agent.run_ollama_operation(
        "ollama_chat", venture_id="v-test", messages=[{"role": "user", "content": "Reply with exactly: pong"}], capability="cheap-fast"
    )
    check("chat completed via the Model Router", chat_entry["event"] == "ollama_completed")
    check("chat produced non-empty content", bool(chat_entry.get("content")))
    print(f"     served by provider/model: {chat_entry.get('provider')}/{chat_entry.get('model')}")

    print("\n== 12. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    ollama_agent.run_ollama_operation("ollama_health_check", venture_id="v-test")
    check("published ollama_started", "ollama_started" in seen_events)
    check("published ollama_completed", "ollama_completed" in seen_events)

    seen_events.clear()
    set_ollama_ops_provider(_AlwaysFailingProvider())
    try:
        ollama_agent.run_ollama_operation("ollama_health_check", venture_id="v-test")
    finally:
        set_ollama_ops_provider(RealOllamaOpsProvider())
    check("published ollama_failed", "ollama_failed" in seen_events)

    print("\n== 13. Manager-callable node shape ==")
    delta = ollama_agent.ollama_ops_node({"venture_id": "v-test"})
    check("ollama_ops_node returns a history delta", "history" in delta and len(delta["history"]) == 1)

    print("\nAll Phase 3 Component 1 checks passed.")


if __name__ == "__main__":
    main()
