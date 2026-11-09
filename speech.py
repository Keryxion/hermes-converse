"""The one module that imports Hermes core speech helpers (STT, TTS, text normalisation).

Everything the plugin needs from core that is not yet on the public plugin API goes through here,
so ``hermes plugins validate`` reports private imports from exactly one file and the upstream PR
that publishes these helpers (``transcribe_audio``, ``resolve_streaming_provider``,
``text_to_speech_tool``) only ever touches this module. Each wrapper looks the core name up at call
time, so a test that patches the core module — or patches this one — is honoured.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Iterator, Optional


logger = logging.getLogger(__name__)

SOURCE_LABEL = "converse"  # the caller-surface label the ``pre_transcription`` hook receives


# ── STT ──

def stt_model_override() -> Optional[str]:
    """STT model override for the converse loop (mirrors the dashboard resolver): the local provider
    prefers ``stt.local.model`` (default ``base``); every other provider uses ``stt.model``."""
    from hermes_cli.config import load_config

    stt = load_config().get("stt") or {}
    if str(stt.get("provider") or "").strip().lower() == "local":
        local = stt.get("local") if isinstance(stt.get("local"), dict) else {}
        return (local or {}).get("model") or "base"
    return stt.get("model")


def transcribe_wav(wav_path: str, *, model: Optional[str] = None) -> str:
    """Transcribe one captured WAV through the configured STT provider and apply the plugin's
    transcript policy: a Whisper hallucination on silence is dropped, ``no_speech`` is an
    empty transcript rather than an error, and a failure is logged and treated as nothing heard."""
    from tools.transcription_tools import transcribe_audio
    from tools.voice_mode_transcript import is_whisper_hallucination

    result = transcribe_audio(wav_path, model=model, source=SOURCE_LABEL)
    if not isinstance(result, dict):
        return ""
    if not result.get("success"):
        if not result.get("no_speech"):
            logger.debug("converse transcription failed: %s", result.get("error"))
        return ""
    transcript = str(result.get("transcript") or "").strip()
    if transcript and is_whisper_hallucination(transcript):
        logger.info("converse: filtered Whisper hallucination %r", transcript)
        return ""
    return transcript


# ── TTS ──

def load_tts_config() -> dict[str, Any]:
    from tools.tts_tool import _load_tts_config

    return _load_tts_config()


def tts_text_cap(tts_config: dict[str, Any]) -> int:
    """The configured provider's per-request max text length."""
    from tools.tts_tool import _get_provider, _resolve_max_text_length

    return _resolve_max_text_length(_get_provider(tts_config), tts_config)


def streaming_provider(tts_config: dict[str, Any]) -> Any:
    """The configured provider's chunked PCM streamer (``.sample_rate`` + ``.stream(text)``), or
    ``None`` when it has no chunked API (edge, the default)."""
    from tools import tts_streaming

    return tts_streaming.resolve_streaming_provider(tts_config)


def text_to_speech(text: str) -> Any:
    """One-shot synthesis through the configured provider: the JSON result envelope
    (``{success, file_path, ...}``) ``text_to_speech_tool`` returns."""
    from tools import tts_tool

    return tts_tool.text_to_speech_tool(text)


def sentence_chunker() -> Any:
    """The incremental sentence cutter every Hermes voice surface uses on LLM deltas."""
    from tools.tts_streaming import SentenceChunker

    return SentenceChunker()


def sentence_boundary_re() -> Any:
    from tools.tts_streaming import SENTENCE_BOUNDARY_RE

    return SENTENCE_BOUNDARY_RE


def strip_markdown(text: str) -> str:
    """The shared TTS cleaner: markdown, emoji, think blocks, units, newlines."""
    from tools.tts_text_normalize import _strip_markdown_for_tts

    return _strip_markdown_for_tts(text)


def mark_speech_interrupted() -> None:
    """Latch the "user interrupted your spoken reply" note the next turn's prompt picks up."""
    from tools.tts_streaming import mark_speech_interrupted as _mark

    _mark()


# ── threads ──

def spawn_context_thread(target: Callable[..., Any], *, name: str, args: tuple = ()) -> Any:
    """An unstarted daemon thread running *target* under the spawner's contextvars, so STT and TTS
    workers read provider credentials through the request's profile scope."""
    from agent.memory_provider import spawn_context_thread as _spawn

    return _spawn(target, name=name, args=args)


def pcm_chunks(streamer: Any, text: str) -> Iterator[bytes]:
    return streamer.stream(text)
