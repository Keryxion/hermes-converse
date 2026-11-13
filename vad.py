"""Voice activity detection for a network audio source: the per-block barge/onset state machine,
the silence endpointer and WAV capture. Pure numpy; no audio device, no Hermes imports.

The detector is the CLI voice mode's design (a quiet-floor calibration, a floor-relative trigger, a
sustained-window trip, a playback grace window) with the knobs the converse loop needs exposed as
parameters: a lower trip fraction so soft speech with inter-syllable dips is heard, a MEDIAN floor
so a loud burst doesn't lock the floor high, and no onset ceiling so a loud ambient (a TV at the
mic) raises the trigger above the noise instead of being pinned below it.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import wave
from collections import deque
from contextlib import suppress
from typing import Any, Optional

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000  # Whisper-native; the default capture rate
SILENCE_RMS_THRESHOLD = 200  # RMS below this = silence (int16 range 0-32767)
# Playback (barge-in) trigger clamps: speaker bleed alone must never trip, and a noisy room must
# never push the trigger past normal speech (3000-8000 RMS; a quiet-room floor is ~50-300).
PLAYBACK_MIN_TRIGGER = 1500.0
TRIGGER_CEILING = 4000.0
# Onset (first-word) trigger clamps. A browser mic with AGC/noise-suppression pumps the between-word
# floor toward 1500-2000, which with floor*mult would pin the trigger at TRIGGER_CEILING and leave
# normal speech below it: a separate, lower onset ceiling keeps first-word detection sensitive.
ONSET_TRIGGER_CEILING = 2000.0
# Absolute floor for the onset trigger: just above the silence line, NOT high enough to deafen a
# low-gain mic whose speech is soft in absolute terms but well above its own noise floor. Brief
# noise is rejected by the sustained trip window, not by a tall absolute wall.
ONSET_TRIGGER_MIN = SILENCE_RMS_THRESHOLD * 1.5  # 300
# Smallest value the adaptive quiet floor is clamped to: well below SILENCE so a low-gain mic's real
# room level (~30-80 RMS) is tracked instead of being inflated to 200.
MIN_QUIET_FLOOR = 40.0
DEFAULT_BARGE_MULTIPLIER = 3.0  # over the quiet floor: 300 * 3 = 900 vs 3000+ speech

# ── converse profile (30 ms blocks at the capture rate) ──
BLOCK_MS = 30
SUSTAINED_MS = 300
# Fraction of the sustained window that must be above the trigger to count as speech onset. Lower
# than the CLI barge default (0.8) so soft speech with inter-syllable dips is still heard.
CONVERSE_TRIP_FRACTION = 0.6
# Self-calibrating floor for noisy rooms: a MEDIAN floor tracks the typical ambient without being
# inflated by loud bursts; onset trigger = floor * 1.5 with no ceiling sits just above the ambient
# so a TV baseline doesn't trip but a louder voice does.
CONVERSE_FLOOR_PERCENTILE = 50.0
CONVERSE_ONSET_MULT = 1.5
# The end-of-utterance threshold is floor-relative too: an utterance ends when the level falls back
# toward the ambient floor, so it closes when the USER stops even if the TV keeps going. max() with
# the absolute threshold keeps the quiet-room behaviour (floor ~50 -> ~200, unchanged).
CONVERSE_ENDPOINT_FLOOR_MULT = 1.3
CALIBRATION_MS = 450
GRACE_MS = 500
PRE_ROLL_MS = 1200
# Hard cap on one capture, so "listening" can never run to half a minute in a pathological room.
MAX_UTTERANCE_MS = 12_000


def rms(np: Any, data: Any) -> float:
    return float(np.sqrt(np.mean(data.astype(np.float64) ** 2)))


def vad_log(msg: str) -> None:
    """VAD diagnostic: logger.debug, plus stderr when HERMES_VOICE_DEBUG=1 (the live-tuning channel
    shared with the CLI voice mode)."""
    logger.debug(msg)
    if os.environ.get("HERMES_VOICE_DEBUG", "").strip() == "1":
        with suppress(Exception):
            print(f"[converse-vad] {msg}", file=sys.stderr, flush=True)


def write_wav(np: Any, audio: Any, *, sample_rate: int = SAMPLE_RATE) -> str:
    """Write int16 mono *audio* to a temp WAV at *sample_rate*; return its path."""
    fd, path = tempfile.mkstemp(prefix="converse-", suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(np.asarray(audio, dtype=np.int16).tobytes())
    return path


def unlink_quietly(path: Optional[str]) -> None:
    """Best-effort unlink; missing/undeletable files are ignored."""
    if path:
        with suppress(OSError):
            os.unlink(path)


def utterance_volume(np: Any, audio: Any) -> float:
    """The RMS loudness of a captured utterance, same int16-amplitude units as every threshold in
    this module (a quiet room is ~30-80, normal speech ~3000-8000). This is a local
    capture measurement, separate from the normalized 0–1 wake volume reported by clients."""
    return rms(np, audio)


class BargeDetector:
    """Per-block speech onset / barge-in state machine.

    Feed one block's RMS at a time with the playback flag; the first ``calib_blocks`` quiet blocks
    lock the floor. ``feed`` returns the phase name (``"generation"`` while nothing plays,
    ``"playback"`` during TTS) when speech trips, else ``None``. Defaults are the CLI voice mode's
    profile; :func:`converse_detector` builds the converse profile.
    """

    def __init__(self, np: Any, *, mult: float, calib_blocks: int, trip_blocks: int, grace_blocks: int,
                 trip_fraction: float = 0.8, floor_percentile: float = 90.0,
                 onset_mult: Optional[float] = None,
                 onset_ceiling: Optional[float] = ONSET_TRIGGER_CEILING) -> None:
        self._np, self.mult, self.calib_blocks, self.grace_blocks = np, mult, calib_blocks, grace_blocks
        self.trip_needed = max(1, round(trip_blocks * trip_fraction))
        self._floor_percentile = float(floor_percentile)
        self._onset_mult = float(onset_mult) if onset_mult else float(mult)
        self._onset_ceiling = onset_ceiling
        self.ambient: deque = deque(maxlen=100)  # ~3s of quiet-phase RMS
        self.recent_above: deque = deque(maxlen=trip_blocks)
        self.quiet_floor = float(SILENCE_RMS_THRESHOLD)
        self.floor_locked = self.playing_prev = self.playback_seen = False
        self.grace_remaining = self.block_idx = 0
        self.blocks_since_playback = 10_000

    def _floor(self) -> tuple:
        """(percentile, floor): the configured percentile of the quiet window, clamped to a small
        positive ``MIN_QUIET_FLOOR`` so the floor can track the real level (30-80 in a quiet room,
        ~1500 with a TV on); the trigger stays safe via ONSET_TRIGGER_MIN + the sustained window."""
        pctile = (float(self._np.percentile(list(self.ambient), self._floor_percentile))
                  if self.ambient else float(MIN_QUIET_FLOOR))
        return pctile, max(pctile, float(MIN_QUIET_FLOOR))

    def _calibrate(self, rms_value: float, playing: bool) -> bool:
        """Lock the floor from the first calib_blocks: they sample the quiet room, not bleed."""
        if not playing:
            self.ambient.append(rms_value)
        if len(self.ambient) >= self.calib_blocks or playing:
            pct, self.quiet_floor = self._floor()
            self.floor_locked = True
            vad_log(f"calibrated quiet floor={self.quiet_floor:.0f} "
                    f"(pct={pct:.0f}, {len(self.ambient)} blocks, mult={self.mult:g})")
        return self.floor_locked

    def _track_playback(self, playing: bool) -> None:
        """Grace only when playback starts after a real gap (>=1s), so inter-sentence flapping of
        the audio-active flag can't chain windows and swallow an interjection."""
        if playing and not self.playing_prev:
            if not self.playback_seen or self.blocks_since_playback > 33:
                self.grace_remaining = self.grace_blocks
                vad_log(f"playback started (block={self.block_idx}) — grace {self.grace_blocks * BLOCK_MS}ms")
            self.playback_seen = True
        self.playing_prev = playing
        self.blocks_since_playback = 0 if playing else self.blocks_since_playback + 1

    def feed(self, rms_value: float, playing: bool, *, interrupting: bool = False) -> Optional[str]:
        """Consume a block; active-turn interruption uses stricter energy than idle onset."""
        self.block_idx += 1
        if not self.floor_locked and not self._calibrate(rms_value, playing):
            return None
        self._track_playback(playing)
        # Trigger: quiet baseline x multiplier, phase-dependent.
        #  • Active-turn interruption: quiet_floor*mult, floored at PLAYBACK_MIN_TRIGGER and capped at
        #    TRIGGER_CEILING so a noisy room never trips on speaker bleed.
        #  • Ordinary onset: quiet_floor*onset_mult, floored at ONSET_TRIGGER_MIN and capped
        #    at onset_ceiling IF one is set (the converse profile sets none).
        if playing or interrupting:
            trigger = min(max(self.quiet_floor * self.mult, PLAYBACK_MIN_TRIGGER), TRIGGER_CEILING)
        else:
            trigger = max(self.quiet_floor * self._onset_mult, ONSET_TRIGGER_MIN)
            if self._onset_ceiling is not None:
                trigger = min(trigger, self._onset_ceiling)
        # Track ambient drift ONLY while nothing plays (never absorb bleed) and ONLY for blocks near
        # the quiet estimate. A block well above the floor is speech, not ambient — absorbing it
        # ratchets the floor up toward speech level until the detector goes deaf.
        drift_gate = min(trigger, max(float(SILENCE_RMS_THRESHOLD), self.quiet_floor * 1.5))
        if not playing and rms_value < drift_gate:
            self.ambient.append(rms_value)
            _, self.quiet_floor = self._floor()
        above = rms_value >= trigger
        if above and self.grace_remaining > 0:
            vad_log(f"grace suppression: block={self.block_idx} rms={rms_value:.0f} "
                    f"trigger={trigger:.0f} ({self.grace_remaining} blocks left)")
            above = False
        if self.grace_remaining > 0:
            self.grace_remaining -= 1
        self.recent_above.append(above)
        phase = "playback" if playing else "generation"
        if rms_value >= trigger * 0.5:
            vad_log(f"block={self.block_idx} rms={rms_value:.0f} floor={self.quiet_floor:.0f} "
                    f"trigger={trigger:.0f} above={above} "
                    f"window={sum(self.recent_above)}/{self.trip_needed} phase={phase}")
        if not (above and sum(self.recent_above) >= self.trip_needed):
            return None
        vad_log(f"energy candidate ({phase}): block={self.block_idx} rms={rms_value:.0f} floor={self.quiet_floor:.0f} "
                f"trigger={trigger:.0f} window={sum(self.recent_above)}/{len(self.recent_above)}")
        return phase


def converse_detector(np: Any, *, mult: Optional[float] = None) -> BargeDetector:
    """The detector the converse loop runs: CLI barge clamps for playback, the self-calibrating
    onset profile for a network mic in a possibly noisy room."""
    return BargeDetector(
        np, mult=float(mult) if mult else DEFAULT_BARGE_MULTIPLIER,
        calib_blocks=max(1, CALIBRATION_MS // BLOCK_MS),
        trip_blocks=max(1, SUSTAINED_MS // BLOCK_MS),
        grace_blocks=max(0, GRACE_MS // BLOCK_MS),
        trip_fraction=CONVERSE_TRIP_FRACTION,
        floor_percentile=CONVERSE_FLOOR_PERCENTILE,
        onset_mult=CONVERSE_ONSET_MULT,
        onset_ceiling=None,
    )
