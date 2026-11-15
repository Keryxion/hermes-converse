"""The session's input level: one gain for a device's microphone, learned from its room's floor.

Speech detection needs speech at a usable absolute level, not only above the noise. Measured on an
Echo Dot 2 (2026-11-15): its front end delivers speech at -38..-55 dBFS over a floor near -83 dBFS,
and Silero rates speech 50 dB under ``tests/fixtures/jfk-4s.wav`` (16 dB over that floor) at most
0.24, so no onset, while the same audio 20 dB louder reaches 0.98. A USB or ESP32 microphone's floor
already sits at or above ``TARGET_FLOOR``, and keeps its level. Clients send raw audio (#138): the
level is the server's to handle, on its own copy; traces report levels as received.

The gain is learned from the quietest blocks heard while the session is idle and held while a
capture, a turn or a reply is under way (specs/audio.md): a voice raised over a reply stays louder
than the reply's echo by as much as it was at the microphone, and no word is louder at its end than
at its start. Pure numpy.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from typing import Any

from . import vad

logger = logging.getLogger(__name__)

# The floor the gain brings a quiet device up to, in int16 RMS of a 30 ms block: the lowest room
# floor the detector is built for (vad.MIN_QUIET_FLOOR), so an ordinary microphone (30-80 on a
# low-gain one, 50-300 on most) keeps its level and only a quieter one is lifted.
TARGET_FLOOR = vad.MIN_QUIET_FLOOR
MAX_GAIN = 100.0              # +40 dB: the Echo's -83 dBFS floor (RMS 2-3) comes up to about -59 dBFS
FLOOR_PERCENTILE = 20.0       # the quiet end of the window: speech fills the rest, the floor its gaps
WINDOW_BLOCKS = 200           # 6 s of idle blocks
MIN_BLOCKS = 10               # 300 ms before the gain leaves 1
LOG_STEP_DB = 3.0             # a change this large is logged
DIGITAL_SILENCE = 1.0         # blocks under this RMS are not the room


class InputLevel:
    def __init__(self, np: Any) -> None:
        self._np = np
        self._idle: deque = deque(maxlen=WINDOW_BLOCKS)
        self.gain = 1.0
        self._logged_db = 0.0

    @property
    def gain_db(self) -> float:
        return 20 * math.log10(self.gain)

    def apply(self, block: Any, *, learn: bool) -> Any:
        """The block at the session's gain. ``learn``: the session is idle, so this block may move it."""
        np = self._np
        rms = float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))
        # Digital silence (a client's padding, a muted stream) says nothing about the room: a real
        # microphone's floor is never under 1 RMS (the Echo's is 2-3)
        if learn and rms >= DIGITAL_SILENCE:
            self._idle.append(rms)
            if len(self._idle) >= MIN_BLOCKS:
                floor = max(float(np.percentile(self._idle, FLOOR_PERCENTILE)), TARGET_FLOOR / MAX_GAIN)
                self.gain = min(MAX_GAIN, max(1.0, TARGET_FLOOR / floor))
                if abs(self.gain_db - self._logged_db) >= LOG_STEP_DB:
                    self._logged_db = self.gain_db
                    logger.info("converse input: room floor %.1f RMS, gain %+.1f dB", floor, self.gain_db)
        if self.gain == 1.0:
            return block
        return np.clip(np.round(block.astype(np.float64) * self.gain), -32768, 32767).astype(np.int16)
