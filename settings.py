"""Plugin settings: ``plugins.entries.hermes-converse.settings.*`` read through the plugin context.

Every value has a default so a fresh install works with no configuration. ``load_settings`` takes
the getter rather than the context so tests (and the CLI) can feed it a dict.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .phrases import normalize_phrases
from .arbitrate import DEFAULT_WAKE_WINDOW, POLICY_FIRST_TO_ENDPOINT, POLICY_WAKE_SIGNAL
from .speech_detector import DEFAULT_VAD_MODEL

DEFAULT_NAME = "Arya"
DEFAULT_END_PHRASES = ("over and out",)
# ── cross-device voice arbitration ────────────────────────────────────────────────────────
DEFAULT_ARBITRATE_POLICY = POLICY_WAKE_SIGNAL
DEFAULT_ARBITRATE_DEVICE_ID = None  # ''/None = auto-generate from socket
DEFAULT_ARBITRATE_ENABLED = False
# How long the arbitrator collects concurrent wake claims for the same wake word before electing
# the primary device. Long enough to cover detector jitter across devices, short enough that a
# client is not left waiting before it starts streaming.
DEFAULT_ARBITRATE_WAKE_WINDOW_SECONDS = DEFAULT_WAKE_WINDOW
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
# Received silence that makes a candidate pause: the fixed endpoint, and Smart Turn's first check.
DEFAULT_ENDPOINT_SILENCE_MS = 700
# Longest pause the user may take inside a request: Smart Turn keeps listening through an
# incomplete-sounding pause until this much silence, and a wake activation waits this long for the
# request after the wake word.
DEFAULT_ENDPOINT_MAX_SILENCE_MS = 3000
# Capture-only wall-clock timeout; idle quiet still counts received audio.
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
    end_phrases: tuple[str, ...] = DEFAULT_END_PHRASES
    endpoint_model: bool = False
    endpoint_threshold: float = DEFAULT_ENDPOINT_THRESHOLD
    endpoint_silence_ms: int = DEFAULT_ENDPOINT_SILENCE_MS
    endpoint_max_silence_ms: int = DEFAULT_ENDPOINT_MAX_SILENCE_MS
    capture_no_data_seconds: float = DEFAULT_CAPTURE_NO_DATA_SECONDS
    vad_model: str = DEFAULT_VAD_MODEL
    approval_choice: str = "deny"
    api_base_url: str = ""
    # Cooperative wake recommendations; never suppress streamed audio.
    arbitrate_enabled: bool = DEFAULT_ARBITRATE_ENABLED
    arbitrate_policy: str = DEFAULT_ARBITRATE_POLICY
    arbitrate_device_id: Optional[str] = DEFAULT_ARBITRATE_DEVICE_ID
    arbitrate_wake_window_seconds: float = DEFAULT_ARBITRATE_WAKE_WINDOW_SECONDS


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


def _arbitrate_policy(raw: Any) -> str:
    """Coerce *raw* to a known arbitrate policy, falling back to the default on anything unknown."""
    policy = str(raw or DEFAULT_ARBITRATE_POLICY).strip()
    if policy not in (POLICY_FIRST_TO_ENDPOINT, POLICY_WAKE_SIGNAL):
        return DEFAULT_ARBITRATE_POLICY
    return policy


def _wake_window(raw: Any) -> float:
    value = _number(raw, DEFAULT_ARBITRATE_WAKE_WINDOW_SECONDS)
    return value if math.isfinite(value) and value >= 0 else DEFAULT_ARBITRATE_WAKE_WINDOW_SECONDS


def _capture_timeout(raw: Any) -> float:
    value = _number(raw, DEFAULT_CAPTURE_NO_DATA_SECONDS)
    return max(0.0, value) if math.isfinite(value) else DEFAULT_CAPTURE_NO_DATA_SECONDS


def _milliseconds(raw: Any, default: int) -> int:
    value = _number(raw, default)
    return int(value) if math.isfinite(value) and value > 0 else default


def load_settings(get: Getter) -> Settings:
    """Build :class:`Settings` from ``get(key, default)`` (``ctx.get_config`` or a dict's ``.get``)."""
    approval = str(get("approval_choice", "deny") or "deny").strip().lower()
    silence_ms = _milliseconds(get("endpoint_silence_ms", DEFAULT_ENDPOINT_SILENCE_MS), DEFAULT_ENDPOINT_SILENCE_MS)
    return Settings(
        name=_text(get("name", DEFAULT_NAME)) or DEFAULT_NAME,
        model=_text(get("voice_model", "")),
        provider=_text(get("voice_provider", "")),
        turn_timeout=_number(get("turn_timeout_seconds", DEFAULT_TURN_TIMEOUT), DEFAULT_TURN_TIMEOUT),
        tail_fallback=_number(get("playback_tail_timeout_seconds", DEFAULT_TAIL_FALLBACK), DEFAULT_TAIL_FALLBACK),
        max_tts_chars=int(_number(get("max_tts_chars_per_turn", DEFAULT_MAX_TTS_CHARS), DEFAULT_MAX_TTS_CHARS)),
        end_phrases=normalize_phrases(get("end_phrases", DEFAULT_END_PHRASES), DEFAULT_END_PHRASES),
        endpoint_model=_model_enabled(get("endpoint_model", "")),
        vad_model="energy" if str(get("vad_model", DEFAULT_VAD_MODEL)).strip().lower() == "energy" else "silero",
        endpoint_threshold=min(1.0, max(0.0, _number(get("endpoint_threshold", DEFAULT_ENDPOINT_THRESHOLD),
                                                       DEFAULT_ENDPOINT_THRESHOLD))),
        endpoint_silence_ms=silence_ms,
        endpoint_max_silence_ms=max(silence_ms, _milliseconds(
            get("endpoint_max_silence_ms", DEFAULT_ENDPOINT_MAX_SILENCE_MS), DEFAULT_ENDPOINT_MAX_SILENCE_MS)),
        capture_no_data_seconds=_capture_timeout(
            get("capture_no_data_seconds", DEFAULT_CAPTURE_NO_DATA_SECONDS)),
        approval_choice=approval if approval in APPROVAL_CHOICES else "deny",
        api_base_url=str(get("api_base_url", "") or "").strip().rstrip("/"),
        arbitrate_enabled=_model_enabled(get("arbitrate_enabled", DEFAULT_ARBITRATE_ENABLED)),
        arbitrate_policy=_arbitrate_policy(get("arbitrate_policy", DEFAULT_ARBITRATE_POLICY)),
        arbitrate_device_id=_text(get("arbitrate_device_id", DEFAULT_ARBITRATE_DEVICE_ID)),
        arbitrate_wake_window_seconds=_wake_window(
            get("arbitrate_wake_window_seconds", DEFAULT_ARBITRATE_WAKE_WINDOW_SECONDS)),
    )
