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
at its start.

The room's floor belongs to the device, not to one socket: an Echo reconnects on every server
restart, and its first activation would otherwise start from no gain, its onset coming before
the gain had learned anything (sakura, 2026-11-15: a capture of an Echo's request at 0 dB, Whisper
heard "Thank you."). So the floor is kept per device name, in memory and in
``<HERMES_HOME>/cache/voice/input-levels.json``, and a new session of that device starts from it.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
from collections import deque
from pathlib import Path
from typing import Any, Optional

from . import vad

logger = logging.getLogger(__name__)

# The floor the gain brings a quiet device up to, in int16 RMS of a 30 ms block: the lowest room
# floor the detector is built for (vad.MIN_QUIET_FLOOR), so an ordinary microphone (30-80 on a
# low-gain one, 50-300 on most) keeps its level and only a quieter one is lifted.
TARGET_FLOOR = vad.MIN_QUIET_FLOOR
MAX_GAIN = 100.0              # +40 dB: the Echo's -83 dBFS floor (RMS 2-3) comes up to about -59 dBFS
# The floor falls at once and rises slowly. Blocks are learned until an onset starts a capture, so
# speech the detector does not hear yet is in the window too: with a 6 s window at its 20th
# percentile, a far voice's sentence raised the floor from 4.9 to 33 RMS and the gain from +18 to
# +1.6 dB within it, and the speech went unheard (sakura, 2026-11-15). A long window's low end and a
# rise limit keep a sentence from moving it; a TV left on is followed, at 1 dB a second.
FLOOR_PERCENTILE = 10.0
WINDOW_BLOCKS = 600           # 18 s of idle blocks: a sentence is a third of it at most
RISE_DB_PER_S = 1.0
MIN_BLOCKS = 10               # 300 ms before the gain leaves 1
LOG_STEP_DB = 3.0             # a change this large is logged
DIGITAL_SILENCE = 1.0         # blocks under this RMS are not the room


SAVE_STEP_DB = 1.0            # a floor this much moved is saved


def _store_path() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_home
    except ImportError:  # no Hermes (tests without a checkout): memory only
        return None
    return get_hermes_home() / "cache" / "voice" / "input-levels.json"


_lock = threading.Lock()
_floors: Optional[dict[str, float]] = None   # device name -> room floor (RMS), loaded once


def _load() -> dict[str, float]:
    global _floors
    if _floors is None:
        _floors = {}
        path = _store_path()
        try:
            if path is not None and path.is_file():
                data = json.loads(path.read_text())
                _floors = {k: float(v) for k, v in data.items() if isinstance(k, str) and float(v) > 0}
        except (OSError, ValueError, TypeError, AttributeError):
            logger.warning("converse input: %s unreadable, starting without saved floors", path)
    return _floors


def _save() -> None:
    path = _store_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".input-levels.")
        with os.fdopen(fd, "w") as f:
            json.dump(_floors, f)
        os.replace(tmp, path)
    except OSError:
        logger.warning("converse input: cannot save %s", path, exc_info=True)


class InputLevel:
    def __init__(self, np: Any) -> None:
        self._np = np
        self._idle: deque = deque(maxlen=WINDOW_BLOCKS)
        self.gain = 1.0
        self._logged_db = 0.0
        self._device: Optional[str] = None
        self._saved: Optional[float] = None
        self._floor: Optional[float] = None

    def attach(self, device: Optional[str]) -> None:
        """The session's device: start from its room's floor as last learned, and keep learning it."""
        if not device:
            return
        with _lock:
            floor = _load().get(device)
        self._device, self._saved = device, floor
        if floor is not None and not self._idle:
            self._idle.extend([floor] * MIN_BLOCKS)
            self._floor = floor
            self.gain = min(MAX_GAIN, max(1.0, TARGET_FLOOR / max(floor, TARGET_FLOOR / MAX_GAIN)))
            self._logged_db = self.gain_db
            logger.info("converse input: %s starts from its room floor %.1f RMS, gain %+.1f dB", device, floor, self.gain_db)

    def _remember(self, floor: float) -> None:
        if self._device is None:
            return
        if self._saved is not None and abs(20 * math.log10(floor / self._saved)) < SAVE_STEP_DB:
            return
        self._saved = floor
        with _lock:
            _load()[self._device] = floor
            _save()

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
                if self._floor is not None and floor > self._floor:     # rises slowly, falls at once
                    floor = min(floor, self._floor * 10 ** (RISE_DB_PER_S * vad.BLOCK_MS / 1000 / 20))
                self._floor = floor
                self.gain = min(MAX_GAIN, max(1.0, TARGET_FLOOR / floor))
                self._remember(floor)
                if abs(self.gain_db - self._logged_db) >= LOG_STEP_DB:
                    self._logged_db = self.gain_db
                    logger.info("converse input: room floor %.1f RMS, gain %+.1f dB", floor, self.gain_db)
        if self.gain == 1.0:
            return block
        return np.clip(np.round(block.astype(np.float64) * self.gain), -32768, 32767).astype(np.int16)
