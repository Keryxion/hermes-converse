"""Plugin settings: ``plugins.entries.hermes-converse.settings.*`` read through the plugin context.

Every value has a default so a fresh install works with no configuration. ``load_settings`` takes
the getter rather than the context so tests (and the CLI) can feed it a dict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from .phrases import normalize_phrases

DEFAULT_NAME = "Sakura"
DEFAULT_STOP_PHRASES = ("stop", "cancel")
DEFAULT_END_PHRASES = ("over and out",)
# Hard cap on a single agent turn (LLM think + stream). A turn that produces nothing — an LLM call
# hung with no request timeout — otherwise never ends: no reply, no turn_done, so the client is stuck
# "thinking" forever. The driver stops the run at this deadline, sends an error + turn_done, and
# returns to listening. This is a backstop; the root-cause cap is a provider request timeout.
DEFAULT_TURN_TIMEOUT = 120.0
# How long the server keeps the "playback tail" state — turn_done sent, waiting for the client's
# `drained` — before resuming quiet/VAD on its own. Only a backstop for a client that never sends it.
DEFAULT_TAIL_FALLBACK = 45.0
# Safety cap on how much of ONE reply is ever synthesized, so a runaway reply (a model ignoring the
# brevity prompt, a tool result read aloud) can't play for minutes.
DEFAULT_MAX_TTS_CHARS = 1500
DEFAULT_ENDPOINT_THRESHOLD = 0.5
# How long a capture may wait for audio with nothing arriving before it is abandoned and dropped,
# rather than left open so a much-later stream can complete it and merge two unrelated utterances.
DEFAULT_CAPTURE_NO_DATA_SECONDS = 5.0
APPROVAL_CHOICES = ("deny", "once")

Getter = Callable[..., Any]


@dataclass(frozen=True)
class Settings:
    name: str = DEFAULT_NAME
    model: Optional[str] = None
    provider: Optional[str] = None
    turn_timeout: float = DEFAULT_TURN_TIMEOUT
    tail_fallback: float = DEFAULT_TAIL_FALLBACK
    max_tts_chars: int = DEFAULT_MAX_TTS_CHARS
    stop_phrases: tuple[str, ...] = DEFAULT_STOP_PHRASES
    end_phrases: tuple[str, ...] = DEFAULT_END_PHRASES
    endpoint_model: bool = False
    endpoint_threshold: float = DEFAULT_ENDPOINT_THRESHOLD
    capture_no_data_seconds: float = DEFAULT_CAPTURE_NO_DATA_SECONDS
    approval_choice: str = "deny"
    api_base_url: str = ""


def _number(raw: Any, default: float) -> float:
    try:
        return default if raw is None else float(raw)
    except (TypeError, ValueError):
        return default


def _text(raw: Any) -> Optional[str]:
    text = str(raw).strip() if raw is not None else ""
    return text or None


def _model_enabled(raw: Any) -> bool:
    """``endpoint_model``: any value that is not empty / none / false / off enables Smart Turn."""
    return bool(raw) and str(raw).strip().lower() not in ("", "none", "false", "off", "0")


def load_settings(get: Getter) -> Settings:
    """Build :class:`Settings` from ``get(key, default)`` (``ctx.get_config`` or a dict's ``.get``)."""
    approval = str(get("approval_choice", "deny") or "deny").strip().lower()
    return Settings(
        name=_text(get("name", DEFAULT_NAME)) or DEFAULT_NAME,
        model=_text(get("voice_model", "")),
        provider=_text(get("voice_provider", "")),
        turn_timeout=_number(get("turn_timeout_seconds", DEFAULT_TURN_TIMEOUT), DEFAULT_TURN_TIMEOUT),
        tail_fallback=_number(get("playback_tail_timeout_seconds", DEFAULT_TAIL_FALLBACK), DEFAULT_TAIL_FALLBACK),
        max_tts_chars=int(_number(get("max_tts_chars_per_turn", DEFAULT_MAX_TTS_CHARS), DEFAULT_MAX_TTS_CHARS)),
        stop_phrases=normalize_phrases(get("stop_phrases", DEFAULT_STOP_PHRASES), DEFAULT_STOP_PHRASES),
        end_phrases=normalize_phrases(get("end_phrases", DEFAULT_END_PHRASES), DEFAULT_END_PHRASES),
        endpoint_model=_model_enabled(get("endpoint_model", "")),
        endpoint_threshold=min(1.0, max(0.0, _number(get("endpoint_threshold", DEFAULT_ENDPOINT_THRESHOLD),
                                                      DEFAULT_ENDPOINT_THRESHOLD))),
        capture_no_data_seconds=_number(get("capture_no_data_seconds", DEFAULT_CAPTURE_NO_DATA_SECONDS),
                                        DEFAULT_CAPTURE_NO_DATA_SECONDS),
        approval_choice=approval if approval in APPROVAL_CHOICES else "deny",
        api_base_url=str(get("api_base_url", "") or "").strip().rstrip("/"),
    )
