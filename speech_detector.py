"""One per-connection speech detector for onset and capture silence.

The energy detector stays warm as a fallback. Only feed() or is_quiet() evaluates a
block; callers reading quiet after feed() reuse its result rather than advancing
Silero's recurrent state twice for the same audio.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Any, Optional

from . import vad
from .silero_vad import SileroStream, load_silero_session

logger = logging.getLogger(__name__)
DEFAULT_VAD_MODEL = "silero"
SPEECH_ON = 0.5
SPEECH_OFF = 0.35
MIN_SPEECH_MS = 250
ECHO_MARGIN = 1.5
_LEVEL_WINDOW_BLOCKS = 10  # 300 ms window RMS
_ECHO_LAG_BLOCKS = 10  # reference excludes the newest 300 ms (longer than the onset run)
_ECHO_MIN_BLOCKS = 6  # plus lag: 16 blocks / 480 ms before a baseline is available


class SpeechDetector:
    def __init__(self, np: Any, *, model: str = DEFAULT_VAD_MODEL, input_rate: int = vad.SAMPLE_RATE,
                 mult: Optional[float] = None) -> None:
        self._np, self._input_rate = np, input_rate
        self._energy = vad.converse_detector(np, mult=mult)
        self._stream = None
        self.fallback_reason: Optional[str] = None
        self.quiet = True
        self.probability: Optional[float] = None
        self._in_speech = False
        self._speech_ms = 0.0
        self._levels: deque = deque(maxlen=_LEVEL_WINDOW_BLOCKS)
        self._echo: deque = deque(maxlen=100 + _ECHO_LAG_BLOCKS)
        if model != "energy":
            session, self.fallback_reason = load_silero_session()
            if session is not None:
                try:
                    self._stream = SileroStream(session, np, input_rate=input_rate)
                except Exception as exc:
                    self.fallback_reason = f"stream unavailable ({type(exc).__name__})"
            if self.fallback_reason:
                logger.warning("converse VAD: Silero unavailable (%s); using energy", self.fallback_reason)

    @property
    def name(self) -> str:
        return "silero" if self._stream is not None else "energy"

    def _probability(self, block: Any) -> Optional[float]:
        if self._stream is None:
            return None
        try:
            return self._stream.feed(block)
        except Exception as exc:
            self.fallback_reason = f"inference failed ({type(exc).__name__})"
            self._stream = None
            logger.warning("converse VAD: %s; using energy", self.fallback_reason)
            return None

    def _energy_quiet(self, block: Any) -> bool:
        threshold = max(float(vad.SILENCE_RMS_THRESHOLD),
                        self._energy.quiet_floor * vad.CONVERSE_ENDPOINT_FLOOR_MULT)
        return vad.rms(self._np, block) < threshold

    def feed(self, block: Any, playing: bool, *, interrupting: bool = False) -> Optional[str]:
        """Process once; interrupting selects active-turn sensitivity, even before playback."""
        energy_phase = self._energy.feed(vad.rms(self._np, block), playing, interrupting=interrupting)
        probability = self.probability = self._probability(block)
        self.quiet = self._energy_quiet(block) if probability is None else probability < SPEECH_OFF
        if probability is None:
            return energy_phase
        voiced = heard = probability >= (SPEECH_OFF if self._in_speech else SPEECH_ON)
        level = gate = None
        if playing:
            self._levels.append(float(self._np.mean(block.astype(self._np.float64) ** 2)))
            level = float(self._np.sqrt(self._np.mean(self._levels)))
            settled = list(self._echo)[:-_ECHO_LAG_BLOCKS]
            baseline = (float(self._np.percentile(settled, 90))
                        if len(settled) >= _ECHO_MIN_BLOCKS else None)
            self._echo.append(level)
            gate = None if baseline is None else max(baseline, vad.MIN_QUIET_FLOOR) * ECHO_MARGIN
            voiced = voiced and gate is not None and level >= gate
        else:
            self._levels.clear()
            self._echo.clear()
        self._in_speech = voiced
        self._speech_ms = self._speech_ms + block.size * 1000 / self._input_rate if voiced else 0.0
        if self._speech_ms < MIN_SPEECH_MS:
            if energy_phase is not None:
                # An energy candidate Silero did not confirm: say which condition held it back.
                reason = ("not_voiced" if not heard else "echo_baseline_warming" if playing and gate is None
                          else "below_echo_gate" if not voiced else "speech_too_short")
                vad.vad_log(f"candidate rejected ({energy_phase}): reason={reason} "
                            f"probability={probability:.2f} speech_ms={self._speech_ms:.0f}/{MIN_SPEECH_MS} "
                            f"level={'-' if level is None else f'{level:.0f}'} "
                            f"echo_gate={'-' if gate is None else f'{gate:.0f}'}")
            return None
        # Silero answers "is this speech?", not "is this a sustained, audible
        # interruption?" Require the warm energy detector's sustained threshold
        # during an active turn. A loud click followed by quiet mumbling must
        # not borrow the click's rolling RMS to cancel the reply.
        if (playing or interrupting) and energy_phase is None:
            return None
        self._in_speech, self._speech_ms = False, 0.0
        return "playback" if playing else "generation"

    def is_quiet(self, block: Any) -> bool:
        """Process a capture block once, using the same model for candidate pauses."""
        probability = self.probability = self._probability(block)
        self.quiet = self._energy_quiet(block) if probability is None else probability < SPEECH_OFF
        return self.quiet
