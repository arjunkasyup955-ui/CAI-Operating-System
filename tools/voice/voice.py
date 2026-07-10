from pydantic import BaseModel

from core.registries import ToolSpec, get_tool_registry
from core.registries.tool_registry import RetryPolicy
from tools.voice.voice_providers import get_voice_provider

_RETRY_POLICY = RetryPolicy(max_attempts=2, backoff_seconds=1.0)
_TIMEOUT_SECONDS = 20.0


class HealthCheckArgs(BaseModel):
    pass


class SpeechToTextArgs(BaseModel):
    audio_data: str
    language: str = "en"
    stream: bool = False


class TranscribeAudioArgs(BaseModel):
    audio_data: str
    language: str = "en"
    include_segments: bool = True


class TextToSpeechArgs(BaseModel):
    text: str
    voice: str = "default"
    language: str = "en"
    stream: bool = False


class SynthesizeAudioArgs(BaseModel):
    text: str
    voice: str = "default"
    language: str = "en"
    audio_format: str = "mp3"
    speed: float = 1.0


class DetectActivityArgs(BaseModel):
    audio_data: str


def voice_health_check() -> dict:
    return get_voice_provider().health_check().model_dump()


def voice_speech_to_text(audio_data: str, language: str = "en", stream: bool = False) -> dict:
    return get_voice_provider().speech_to_text(audio_data, language, stream).model_dump()


def voice_transcribe_audio(audio_data: str, language: str = "en", include_segments: bool = True) -> dict:
    return get_voice_provider().transcribe_audio(audio_data, language, include_segments).model_dump()


def voice_text_to_speech(text: str, voice: str = "default", language: str = "en", stream: bool = False) -> dict:
    return get_voice_provider().text_to_speech(text, voice, language, stream).model_dump()


def voice_synthesize_audio(text: str, voice: str = "default", language: str = "en", audio_format: str = "mp3", speed: float = 1.0) -> dict:
    return get_voice_provider().synthesize_audio(text, voice, language, audio_format, speed).model_dump()


def voice_detect_activity(audio_data: str) -> dict:
    return get_voice_provider().detect_activity(audio_data).model_dump()


_TOOLS: list[tuple[str, str, type[BaseModel], object]] = [
    ("voice_health_check", "Check whether the voice provider is configured and reachable", HealthCheckArgs, voice_health_check),
    ("voice_speech_to_text", "Transcribe short audio to text (optionally streamed)", SpeechToTextArgs, voice_speech_to_text),
    ("voice_transcribe_audio", "Full audio transcription with optional timestamped segments", TranscribeAudioArgs, voice_transcribe_audio),
    ("voice_text_to_speech", "Synthesize speech audio from text (optionally streamed)", TextToSpeechArgs, voice_text_to_speech),
    ("voice_synthesize_audio", "Synthesize speech audio with configurable format/speed", SynthesizeAudioArgs, voice_synthesize_audio),
    ("voice_detect_activity", "Detect speech activity/segments in audio", DetectActivityArgs, voice_detect_activity),
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
