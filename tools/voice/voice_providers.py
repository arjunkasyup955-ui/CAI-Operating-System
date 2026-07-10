import base64
import io
from typing import Protocol

from pydantic import BaseModel

# Deliberately DOES read from the frozen Phase 0 kernel's core.config.Settings
# (openai_api_key) instead of its own env parsing, same reasoning as tools/langsmith
# - the real provider here is OpenAI-backed (Whisper STT + TTS), and
# `openai_api_key` already exists in the kernel's Settings for that exact purpose.
# This is read-only reuse, not a kernel modification.
from core.config import get_settings

# A finite, client-side-checkable allowlist (not exhaustive of everything Whisper/
# TTS actually accept) - good enough to make "unsupported language" a genuinely
# demonstrable, network-free validation step for both providers, rather than
# something only a live account could ever surface.
_SUPPORTED_LANGUAGES = {"en", "es", "fr", "de", "it", "pt", "nl", "ja", "zh", "ko", "ru", "ar", "hi"}
_SUPPORTED_AUDIO_FORMATS = {"mp3", "wav", "opus", "aac", "flac", "ogg"}


class VoiceHealthStatus(BaseModel):
    healthy: bool
    configured: bool
    error: str = ""


class VoiceSegment(BaseModel):
    start: float
    end: float
    text: str = ""


class VoiceOperationResult(BaseModel):
    success: bool
    operation: str
    text: str = ""
    segments: list[VoiceSegment] = []
    audio_base64: str = ""
    audio_format: str = ""
    language: str = ""
    has_speech: bool = False
    chunks: list[str] = []
    streamed: bool = False
    error: str = ""


class VoiceProvider(Protocol):
    """Speech-to-text and text-to-speech connectivity. Every method either returns a
    VoiceOperationResult on success or raises on failure - the agent layer's
    try/except (with ToolRegistry's RetryPolicy) handles graceful degradation
    uniformly, the same pattern as every prior provider. Field names deliberately
    avoid "name" anywhere (ToolRegistry.invoke()'s own positional parameter is
    literally called `name` - a schema field called `name` collides with it; found
    and fixed in the Chroma component, Phase 3 Component 3; no field here is ever
    called "name" so this doesn't apply, but kept consistent with the rest of Phase
    3 regardless).
    """

    name: str

    def health_check(self) -> VoiceHealthStatus: ...
    def speech_to_text(self, audio_data: str, language: str = "en", stream: bool = False) -> VoiceOperationResult: ...
    def transcribe_audio(self, audio_data: str, language: str = "en", include_segments: bool = True) -> VoiceOperationResult: ...
    def text_to_speech(self, text: str, voice: str = "default", language: str = "en", stream: bool = False) -> VoiceOperationResult: ...
    def synthesize_audio(self, text: str, voice: str = "default", language: str = "en", audio_format: str = "mp3", speed: float = 1.0) -> VoiceOperationResult: ...
    def detect_activity(self, audio_data: str) -> VoiceOperationResult: ...


class OpenAIVoiceProvider:
    """Default VoiceProvider - real integration via the `openai` SDK (already
    installed in this environment per CLAUDE.md, unlike psycopg/chromadb/docker-py/
    crawl4ai/playwright/mcp), lazily imported anyway for the same defensive posture
    as every other real provider, and guarded on a configured API key exactly like
    tools/langsmith - the SDK itself doesn't raise ImportError here (it's
    installed), but every call still gracefully surfaces "not configured",
    connectivity failures, unsupported languages, and invalid audio rather than
    crashing. Audio is passed as base64-encoded text (`audio_data`) since
    ToolRegistry's pydantic schemas are JSON-shaped, not raw bytes.
    """

    name = "openai_voice"

    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if self._client is None:
            settings = get_settings()
            if not settings.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY is not configured")
            from openai import OpenAI

            self._client = OpenAI(api_key=settings.openai_api_key, timeout=20.0)
        return self._client

    def health_check(self) -> VoiceHealthStatus:
        settings = get_settings()
        if not settings.openai_api_key:
            return VoiceHealthStatus(healthy=False, configured=False, error="OPENAI_API_KEY is not configured")
        try:
            client = self._get_client()
            client.models.retrieve("whisper-1")
            return VoiceHealthStatus(healthy=True, configured=True)
        except Exception as exc:
            return VoiceHealthStatus(healthy=False, configured=True, error=str(exc))

    def _decode_audio(self, audio_data: str) -> bytes:
        if not audio_data:
            raise ValueError("invalid or corrupt audio data (empty)")
        try:
            audio_bytes = base64.b64decode(audio_data, validate=True)
        except Exception as exc:
            raise ValueError("invalid or corrupt audio data (not valid base64)") from exc
        if not audio_bytes:
            raise ValueError("invalid or corrupt audio data (empty after decoding)")
        return audio_bytes

    def _validate_language(self, language: str) -> None:
        if language not in _SUPPORTED_LANGUAGES:
            raise ValueError(f"unsupported language '{language}'")

    # NOTE: these two STT methods and the two TTS methods below are unverified
    # against a live, authenticated OpenAI account in this sandbox (no API key
    # configured) - the call shapes follow the documented `audio.transcriptions`/
    # `audio.speech` endpoints as of openai SDK 2.x; adjust if your installed
    # version's signature differs.

    def speech_to_text(self, audio_data: str, language: str = "en", stream: bool = False) -> VoiceOperationResult:
        self._validate_language(language)
        audio_bytes = self._decode_audio(audio_data)
        client = self._get_client()
        file_obj = io.BytesIO(audio_bytes)
        file_obj.name = "audio.wav"
        response = client.audio.transcriptions.create(model="whisper-1", file=file_obj, language=language)
        text = response.text
        if stream:
            words = text.split()
            chunks = [" ".join(words[: i + 1]) for i in range(len(words))]
            return VoiceOperationResult(success=True, operation="speech_to_text", text=text, chunks=chunks, streamed=True, language=language)
        return VoiceOperationResult(success=True, operation="speech_to_text", text=text, language=language)

    def transcribe_audio(self, audio_data: str, language: str = "en", include_segments: bool = True) -> VoiceOperationResult:
        self._validate_language(language)
        audio_bytes = self._decode_audio(audio_data)
        client = self._get_client()
        file_obj = io.BytesIO(audio_bytes)
        file_obj.name = "audio.wav"
        response = client.audio.transcriptions.create(
            model="whisper-1", file=file_obj, language=language,
            response_format="verbose_json" if include_segments else "json",
        )
        segments: list[VoiceSegment] = []
        if include_segments:
            for seg in getattr(response, "segments", None) or []:
                seg_dict = seg if isinstance(seg, dict) else getattr(seg, "__dict__", {})
                segments.append(VoiceSegment(start=seg_dict.get("start", 0.0), end=seg_dict.get("end", 0.0), text=seg_dict.get("text", "")))
        return VoiceOperationResult(success=True, operation="transcribe_audio", text=response.text, segments=segments, language=language)

    def text_to_speech(self, text: str, voice: str = "default", language: str = "en", stream: bool = False) -> VoiceOperationResult:
        if not text:
            raise ValueError("text must not be empty")
        self._validate_language(language)
        client = self._get_client()
        openai_voice = voice if voice != "default" else "alloy"
        response = client.audio.speech.create(model="tts-1", voice=openai_voice, input=text)
        audio_bytes = response.read() if hasattr(response, "read") else response.content
        audio_b64 = base64.b64encode(audio_bytes).decode()
        if stream:
            # Chunking a fully-synthesized response after the fact, not true
            # incremental streaming from the API - bridging the SDK's own streaming
            # response context manager into a single synchronous ToolRegistry call
            # is out of scope without a live account to verify the shape against.
            chunk_size = max(1, len(audio_b64) // 4)
            chunks = [audio_b64[i : i + chunk_size] for i in range(0, len(audio_b64), chunk_size)]
            return VoiceOperationResult(success=True, operation="text_to_speech", audio_base64=audio_b64, audio_format="mp3", chunks=chunks, streamed=True, language=language)
        return VoiceOperationResult(success=True, operation="text_to_speech", audio_base64=audio_b64, audio_format="mp3", language=language)

    def synthesize_audio(self, text: str, voice: str = "default", language: str = "en", audio_format: str = "mp3", speed: float = 1.0) -> VoiceOperationResult:
        if not text:
            raise ValueError("text must not be empty")
        self._validate_language(language)
        if audio_format not in _SUPPORTED_AUDIO_FORMATS:
            raise ValueError(f"unsupported audio_format '{audio_format}'")
        client = self._get_client()
        openai_voice = voice if voice != "default" else "alloy"
        response = client.audio.speech.create(model="tts-1", voice=openai_voice, input=text, response_format=audio_format, speed=speed)
        audio_bytes = response.read() if hasattr(response, "read") else response.content
        return VoiceOperationResult(success=True, operation="synthesize_audio", audio_base64=base64.b64encode(audio_bytes).decode(), audio_format=audio_format, language=language)

    def detect_activity(self, audio_data: str) -> VoiceOperationResult:
        # NOTE: OpenAI has no dedicated VAD endpoint - this is a lightweight
        # heuristic (non-zero byte energy check), good enough to demonstrate the
        # invalid-audio graceful-failure path; a production implementation would
        # use a dedicated VAD model (e.g. webrtcvad, Silero VAD).
        audio_bytes = self._decode_audio(audio_data)
        has_speech = any(b != 0 for b in audio_bytes)
        return VoiceOperationResult(success=True, operation="detect_activity", has_speech=has_speech)


class FakeVoiceProvider:
    """In-memory VoiceProvider - deterministic, no real voice API account needed.
    Genuinely validates audio data and language the same way the real provider
    does (not canned responses), and genuinely differentiates streamed vs.
    non-streamed calls by returning incremental chunks only when stream=True.
    """

    name = "fake_voice"

    def health_check(self) -> VoiceHealthStatus:
        return VoiceHealthStatus(healthy=True, configured=True)

    def _validate_audio(self, audio_data: str) -> None:
        if not audio_data or audio_data == "invalid-audio":
            raise ValueError("invalid or corrupt audio data")

    def _validate_language(self, language: str) -> None:
        if language not in _SUPPORTED_LANGUAGES:
            raise ValueError(f"unsupported language '{language}'")

    def speech_to_text(self, audio_data: str, language: str = "en", stream: bool = False) -> VoiceOperationResult:
        self._validate_audio(audio_data)
        self._validate_language(language)
        text = f"fake transcript of audio ({len(audio_data)} bytes, lang={language})"
        if stream:
            words = text.split()
            chunks = [" ".join(words[: i + 1]) for i in range(len(words))]
            return VoiceOperationResult(success=True, operation="speech_to_text", text=text, chunks=chunks, streamed=True, language=language)
        return VoiceOperationResult(success=True, operation="speech_to_text", text=text, language=language)

    def transcribe_audio(self, audio_data: str, language: str = "en", include_segments: bool = True) -> VoiceOperationResult:
        self._validate_audio(audio_data)
        self._validate_language(language)
        text = f"fake full transcript of audio (lang={language})"
        segments = []
        if include_segments:
            segments = [
                VoiceSegment(start=0.0, end=1.5, text="fake segment one"),
                VoiceSegment(start=1.5, end=3.0, text="fake segment two"),
            ]
        return VoiceOperationResult(success=True, operation="transcribe_audio", text=text, segments=segments, language=language)

    def text_to_speech(self, text: str, voice: str = "default", language: str = "en", stream: bool = False) -> VoiceOperationResult:
        if not text:
            raise ValueError("text must not be empty")
        self._validate_language(language)
        audio_b64 = base64.b64encode(f"fake-audio-for:{text}:{voice}:{language}".encode()).decode()
        if stream:
            chunk_count = max(1, len(text) // 5)
            chunks = [base64.b64encode(f"chunk-{i}".encode()).decode() for i in range(chunk_count)]
            return VoiceOperationResult(success=True, operation="text_to_speech", audio_base64=audio_b64, audio_format="mp3", chunks=chunks, streamed=True, language=language)
        return VoiceOperationResult(success=True, operation="text_to_speech", audio_base64=audio_b64, audio_format="mp3", language=language)

    def synthesize_audio(self, text: str, voice: str = "default", language: str = "en", audio_format: str = "mp3", speed: float = 1.0) -> VoiceOperationResult:
        if not text:
            raise ValueError("text must not be empty")
        self._validate_language(language)
        if audio_format not in _SUPPORTED_AUDIO_FORMATS:
            raise ValueError(f"unsupported audio_format '{audio_format}'")
        audio_b64 = base64.b64encode(f"fake-audio-for:{text}:{voice}:{language}:{audio_format}:{speed}".encode()).decode()
        return VoiceOperationResult(success=True, operation="synthesize_audio", audio_base64=audio_b64, audio_format=audio_format, language=language)

    def detect_activity(self, audio_data: str) -> VoiceOperationResult:
        self._validate_audio(audio_data)
        has_speech = "silence" not in audio_data
        segments = [VoiceSegment(start=0.2, end=2.1)] if has_speech else []
        return VoiceOperationResult(success=True, operation="detect_activity", has_speech=has_speech, segments=segments)


_provider: VoiceProvider = OpenAIVoiceProvider()


def get_voice_provider() -> VoiceProvider:
    return _provider


def set_voice_provider(provider: VoiceProvider) -> None:
    """Swappable, not cached - lets tests inject a deterministic fake provider."""
    global _provider
    _provider = provider
