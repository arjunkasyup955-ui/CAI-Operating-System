"""Phase 3, Component 11: Voice APIs Integration.

Verifies:
  - Registration (agent + 6 tools)
  - Permissions (internet_access, enforced)
  - Schema validation
  - Retry (ToolRegistry's existing RetryPolicy, via a flaky fake provider)
  - Timeout (ToolRegistry's timeout mechanism, via a deliberately slow function)
  - Graceful failure (real provider - no OPENAI_API_KEY configured - plus invalid
    audio and unsupported language, both network-free validation paths that work
    even without an API key, never crashing)
  - Fake provider STT lifecycle (speech_to_text, transcribe_audio with segments,
    invalid audio and unsupported language failures)
  - Fake provider TTS lifecycle (text_to_speech, synthesize_audio with configurable
    format/speed, empty text and unsupported format failures)
  - Streaming (STT and TTS both return incremental chunks when stream=True and a
    single combined result when stream=False)
  - Voice activity detection (speech vs. silence)
  - Event publishing (voice_started/completed/failed)
  - Manager node integration
  - Regression of every previous component (run separately, see the full suite this
    script is part of)

Run: python scripts/smoke_test_phase3_voice.py
"""

import base64
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from pydantic import ValidationError, create_model  # noqa: E402

import agents.infrastructure.voice.agent as voice_agent  # noqa: E402
from core.event_bus import get_event_bus  # noqa: E402
from core.permissions import PermissionDeniedError  # noqa: E402
from core.registries import ToolSpec, get_agent_registry, get_tool_registry  # noqa: E402
from core.registries.tool_registry import RetryPolicy  # noqa: E402
from tools.voice.voice_providers import (  # noqa: E402
    FakeVoiceProvider,
    OpenAIVoiceProvider,
    VoiceHealthStatus,
    set_voice_provider,
)


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"test failed at: {label}")


class _FlakyProvider:
    name = "flaky"

    def __init__(self, fail_times: int = 1) -> None:
        self.calls = 0
        self._fail_times = fail_times

    def health_check(self) -> VoiceHealthStatus:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("transient failure")
        return VoiceHealthStatus(healthy=True, configured=True)


def main() -> None:
    expected_tools = {
        "voice_health_check", "voice_speech_to_text", "voice_transcribe_audio",
        "voice_text_to_speech", "voice_synthesize_audio", "voice_detect_activity",
    }
    fake_audio = base64.b64encode(b"some-fake-pcm-bytes").decode()

    print("\n== 1. Registration (agent + 6 tools) ==")
    manifest = get_agent_registry().get("voice_agent")
    check("registered under manager", manifest.supervisor == "manager")
    check("declares internet_access permission", set(manifest.permissions) == {"internet_access"})
    check("declares all 6 tools", set(manifest.tools) == expected_tools)
    check("lifecycle is active", manifest.lifecycle == "active")

    print("\n== 2. Permissions ==")
    for tool_name in expected_tools:
        spec = get_tool_registry().get_spec(tool_name)
        check(f"{tool_name} requires internet_access", "internet_access" in spec.permissions)

    manifest_no_perms = manifest.model_copy(update={"name": "no_net_voice_agent", "permissions": []})
    get_agent_registry().register(manifest_no_perms)
    try:
        get_tool_registry().invoke("voice_health_check", agent_name="no_net_voice_agent")
        denied = False
    except PermissionDeniedError:
        denied = True
    check("agent without internet_access is denied", denied)

    try:
        get_tool_registry().invoke("voice_health_check", agent_name="git_agent")
        denied_other = False
    except PermissionDeniedError:
        denied_other = True
    check("an unrelated registered agent (git_agent) is denied", denied_other)

    print("\n== 3. Schema Validation ==")
    try:
        get_tool_registry().invoke("voice_speech_to_text", agent_name="voice_agent")
        rejected = False
    except ValidationError:
        rejected = True
    check("missing required audio_data rejected", rejected)

    try:
        get_tool_registry().invoke("voice_text_to_speech", agent_name="voice_agent")
        rejected2 = False
    except ValidationError:
        rejected2 = True
    check("missing required text rejected", rejected2)

    print("\n== 4. Retry Behaviour (ToolRegistry's existing RetryPolicy) ==")
    flaky = _FlakyProvider(fail_times=1)
    set_voice_provider(flaky)
    try:
        retry_entry = voice_agent.run_voice_operation("voice_health_check", venture_id="v-test")
        check("retried past one transient failure and completed", retry_entry["event"] == "voice_completed")
        check("exactly 2 attempts were made", flaky.calls == 2)
    finally:
        set_voice_provider(OpenAIVoiceProvider())

    print("\n== 5. Timeout (ToolRegistry's timeout mechanism) ==")
    def _slow_query():
        time.sleep(2.0)
        return {"success": True}

    get_tool_registry().register(
        ToolSpec(
            name="voice_test_slow_query",
            input_schema=create_model("SlowQueryArgs"),
            permissions=["internet_access"],
            retry_policy=RetryPolicy(max_attempts=1),
            timeout_seconds=0.3,
        ),
        _slow_query,
    )
    start = time.monotonic()
    try:
        get_tool_registry().invoke("voice_test_slow_query", agent_name="voice_agent")
        timed_out = False
    except TimeoutError:
        timed_out = True
    elapsed = time.monotonic() - start
    check("a slow query times out per the tool's configured timeout_seconds", timed_out)
    check("did not wait for the full 2s operation to finish", elapsed < 1.5)

    print("\n== 6. Graceful Failure (real provider) ==")
    real_health = voice_agent.run_voice_operation("voice_health_check", venture_id="v-test")
    check("real provider health check completes without crashing", real_health["event"] == "voice_completed")
    check("reports unhealthy (no OPENAI_API_KEY configured in this sandbox)", real_health["healthy"] is False)
    check("reports not configured", real_health["configured"] is False)
    print(f"     real provider error (expected): {real_health.get('error')}")

    bad_audio_real = voice_agent.run_voice_operation("voice_speech_to_text", venture_id="v-test", audio_data="not-valid-base64!!!")
    check("real provider rejects invalid audio without needing an API key", bad_audio_real["event"] == "voice_failed")
    print(f"     real provider invalid-audio error (expected): {bad_audio_real.get('error')}")

    bad_lang_real = voice_agent.run_voice_operation("voice_text_to_speech", venture_id="v-test", text="hello", language="xx")
    check("real provider rejects an unsupported language without needing an API key", bad_lang_real["event"] == "voice_failed")
    print(f"     real provider unsupported-language error (expected): {bad_lang_real.get('error')}")

    print("\n== 7. Fake Provider: STT lifecycle ==")
    set_voice_provider(FakeVoiceProvider())
    try:
        stt = voice_agent.run_voice_operation("voice_speech_to_text", venture_id="v-test", audio_data=fake_audio, language="en")
        check("speech_to_text succeeded", stt["success"])
        check("non-streamed speech_to_text has no chunks", stt["streamed"] is False)

        bad_audio = voice_agent.run_voice_operation("voice_speech_to_text", venture_id="v-test", audio_data="invalid-audio")
        check("speech_to_text rejects invalid audio", bad_audio["event"] == "voice_failed")

        bad_lang = voice_agent.run_voice_operation("voice_speech_to_text", venture_id="v-test", audio_data=fake_audio, language="xx")
        check("speech_to_text rejects an unsupported language", bad_lang["event"] == "voice_failed")

        transcript = voice_agent.run_voice_operation("voice_transcribe_audio", venture_id="v-test", audio_data=fake_audio, include_segments=True)
        check("transcribe_audio succeeded", transcript["success"])
        check("transcribe_audio returns timestamped segments", len(transcript["segments"]) == 2)

        no_segments = voice_agent.run_voice_operation("voice_transcribe_audio", venture_id="v-test", audio_data=fake_audio, include_segments=False)
        check("transcribe_audio omits segments when not requested", len(no_segments["segments"]) == 0)

        print("\n== 8. Fake Provider: TTS lifecycle ==")
        tts = voice_agent.run_voice_operation("voice_text_to_speech", venture_id="v-test", text="hello world", voice="default", language="en")
        check("text_to_speech succeeded", tts["success"])
        check("text_to_speech returns audio data", bool(tts["audio_base64"]))
        check("non-streamed text_to_speech has no chunks", tts["streamed"] is False)

        empty_text = voice_agent.run_voice_operation("voice_text_to_speech", venture_id="v-test", text="")
        check("text_to_speech rejects empty text", empty_text["event"] == "voice_failed")

        synth = voice_agent.run_voice_operation("voice_synthesize_audio", venture_id="v-test", text="hi", audio_format="wav", speed=1.5)
        check("synthesize_audio succeeded", synth["success"])
        check("synthesize_audio honors the requested audio_format", synth["audio_format"] == "wav")

        bad_format = voice_agent.run_voice_operation("voice_synthesize_audio", venture_id="v-test", text="hi", audio_format="xyz")
        check("synthesize_audio rejects an unsupported audio_format", bad_format["event"] == "voice_failed")

        print("\n== 9. Streaming ==")
        stt_stream = voice_agent.run_voice_operation("voice_speech_to_text", venture_id="v-test", audio_data=fake_audio, stream=True)
        check("streamed speech_to_text sets streamed=True", stt_stream["streamed"] is True)
        check("streamed speech_to_text returns multiple chunks", len(stt_stream["chunks"]) > 1)

        tts_stream = voice_agent.run_voice_operation("voice_text_to_speech", venture_id="v-test", text="hello world streaming test", stream=True)
        check("streamed text_to_speech sets streamed=True", tts_stream["streamed"] is True)
        check("streamed text_to_speech returns chunks", len(tts_stream["chunks"]) >= 1)

        print("\n== 10. Voice Activity Detection ==")
        vad_speech = voice_agent.run_voice_operation("voice_detect_activity", venture_id="v-test", audio_data=fake_audio)
        check("detect_activity reports speech present", vad_speech["has_speech"] is True)

        vad_silence = voice_agent.run_voice_operation("voice_detect_activity", venture_id="v-test", audio_data="silence-only-audio")
        check("detect_activity reports no speech for silent audio", vad_silence["has_speech"] is False)
    finally:
        set_voice_provider(OpenAIVoiceProvider())

    print("\n== 11. Event Publishing ==")
    seen_events: list[str] = []
    get_event_bus().subscribe("*", lambda e: seen_events.append(e.type))
    voice_agent.run_voice_operation("voice_health_check", venture_id="v-test")
    check("published voice_started", "voice_started" in seen_events)
    check("published voice_completed", "voice_completed" in seen_events)

    seen_events.clear()
    set_voice_provider(_FlakyProvider(fail_times=10))
    try:
        voice_agent.run_voice_operation("voice_health_check", venture_id="v-test")
    finally:
        set_voice_provider(OpenAIVoiceProvider())
    check("published voice_failed", "voice_failed" in seen_events)

    print("\n== 12. Manager-callable node shape ==")
    delta = voice_agent.voice_agent_node({"venture_id": "v-test"})
    check("voice_agent_node returns a history delta", "history" in delta and len(delta["history"]) == 1)
    check("node result reflects the health check operation", delta["history"][0]["operation"] == "voice_health_check")

    print("\nAll Phase 3 Component 11 checks passed.")


if __name__ == "__main__":
    main()
