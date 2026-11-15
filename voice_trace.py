"""Server-side trace helpers for the voice loop: barge-in counters kept per turn and the
interrupt event whose content is settled after the interrupted turn has finished.

Everything here is cheap bookkeeping on values the audio loop already computed. Nothing writes a
record per audio block, and nothing here reads or changes a decision.
"""
from __future__ import annotations

import math

import uuid
from typing import Any, Optional

from .speech_detector import SPEECH_ON


class BargeStats:
    """Totals and peaks for one reply, from its first PCM sent until the device is done playing it."""

    def __init__(self, turn_id: Optional[str]) -> None:
        self.turn_id = turn_id
        self.mic_ms = 0.0
        self.rejected_quiet_ms = 0.0
        self.peak_probability: Optional[float] = None
        self.peak_level: Optional[float] = None
        self.threshold: Optional[float] = None

    def add(self, block_ms: float, level: Optional[float], probability: Optional[float],
            threshold: Optional[float]) -> None:
        self.mic_ms += block_ms
        if level is not None and (self.peak_level is None or level > self.peak_level):
            self.peak_level = level
        if probability is not None and (self.peak_probability is None or probability > self.peak_probability):
            self.peak_probability = probability
        if threshold is not None:
            self.threshold = threshold
        if (probability is not None and level is not None and threshold is not None
                and probability >= SPEECH_ON and level < threshold):
            self.rejected_quiet_ms += block_ms

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"barge.mic_ms": round(self.mic_ms, 1),
                               "barge.rejected_quiet_ms": round(self.rejected_quiet_ms, 1)}
        for key, value in (("barge.peak_probability", self.peak_probability),
                           ("barge.peak_level", self.peak_level), ("barge.threshold", self.threshold)):
            if value is not None:
                out[key] = round(value, 4)
        return out


class SpeechInterrupt:
    """Speech that stopped a reply. Holds the interrupted turn's span open until the speech has
    been transcribed, then records one event with what it turned out to be."""

    def __init__(self, turn_trace: Any, attrs: dict[str, Any]) -> None:
        self.next_turn_id = uuid.uuid4().hex  # the turn the speech becomes, if it is a request
        self._trace = turn_trace
        self._attrs = attrs
        self._done = False

    def resolve(self, result: Optional[str]) -> None:
        """*result*: ``request``, ``empty``, or None when the capture was abandoned."""
        if self._done:
            return
        self._done = True
        attrs = dict(self._attrs)
        if result is not None:
            attrs["interrupt.result"] = result
        if result == "request":
            attrs["interrupt.next_turn_id"] = self.next_turn_id
        try:
            self._trace.event("interrupt", **attrs)
        finally:
            self._trace.release()


def _raw(value: Optional[float], gain: float) -> Optional[float]:
    return None if value is None else value / gain


def begin_speech_interrupt(turn_trace: Any, reply_phase: str, detector: Any,
                           gain: float = 1.0) -> Optional[SpeechInterrupt]:
    """Hold *turn_trace* and describe what the detector saw at the onset; None when there is
    nothing to record on. Levels as received: the detector's over *gain* (level.py)."""
    if turn_trace is None or not turn_trace.hold():
        return None
    attrs: dict[str, Any] = {"interrupt.cause": "speech", "interrupt.phase": reply_phase,
                             "vad.model": getattr(detector, "name", None),
                             "vad.probability": getattr(detector, "probability", None),
                             "vad.level": _raw(getattr(detector, "level", None), gain),
                             "vad.threshold": _raw(getattr(detector, "threshold", None), gain),
                             "input.gain_db": round(20 * math.log10(gain), 1)}
    return SpeechInterrupt(turn_trace, {k: v for k, v in attrs.items() if v is not None})
