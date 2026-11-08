"""Framework-agnostic realtime voice loop: a network mic shim, the VAD→STT session worker, and the
per-turn incremental-TTS driver. Nothing here touches a socket or a web framework, so it is unit
testable with numpy alone.

Pieces:

* :class:`NetworkMicStream` — a ``sounddevice``-shaped shim whose ``.read()`` pulls int16 blocks
  from a thread-safe queue fed by a WebSocket.
* :class:`ConverseSession` — drives the VAD/STT loop on a worker thread: read 30 ms blocks, feed the
  :class:`~.speech_detector.SpeechDetector`, and on a trip capture the utterance, transcribe it and hand the
  transcript to the driver. It also owns the ``playing`` flag and barge-in (a trip while playing cuts
  TTS) and the session-mode quiet / playback-tail accounting.
* :func:`drive_converse_turns` — one agent turn per transcript: announce it, run the turn through
  the host's ``run_turn`` (deltas stream out as they land), speak the reply sentence by sentence.
"""

from __future__ import annotations

import asyncio
from collections import deque
from functools import wraps
import contextlib
import logging
import queue
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterator, Optional

from . import speech, vad
from .speech_detector import DEFAULT_VAD_MODEL, SpeechDetector
from .phrases import is_end_phrase, is_stop_phrase, strip_end_phrase
from .settings import (
    DEFAULT_END_PHRASES, DEFAULT_MAX_TTS_CHARS, DEFAULT_NAME, DEFAULT_STOP_PHRASES,
    DEFAULT_TAIL_FALLBACK, DEFAULT_TURN_TIMEOUT)
from .synth import split_text_for_tts_stream
from .tracing import PhaseRecorder, emit_turn_trace, start_turn

logger = logging.getLogger(__name__)

# Smart Turn: on reaching the fixed silence window we ask the model whether the utterance *sounds*
# finished; a "hold" keeps listening for another window. Cap consecutive holds so a user who
# genuinely stopped mid-phrase is committed after ~3 windows, not at the utterance cap.
MAX_ENDPOINT_HOLDS = 3
# Reply speech: poll for deltas every 0.5 s; after 4 empty polls (~2 s) speak the buffered tail.
IDLE_POLL_SECONDS = 0.5
IDLE_POLLS_TO_FLUSH = 4
# Keep clients with shorter inactivity deadlines alive during agent/TTS waits.
WORKING_FRAME_INTERVAL = 15.0
SMART_TURN_DEFAULT_THRESHOLD = 0.5
# Bound on the per-connection conversation history kept in memory (~20 user+assistant turns): a
# simple tail-cap so a long-lived socket can't grow it without limit. The Runs API owns the real
# transcript (the socket's session_id continues it); this copy backs the sign-off / next_action
# decisions and the latency log.
HISTORY_MAX_MESSAGES = 40
QUIET_INTERVAL_MAX = 3600.0
# Per-connection sample-rate defaults + clamp range. A single-clock device (ESP32) can set
# input_rate == output_rate; browsers keep the 16 kHz-in / 24 kHz-out split.
DEFAULT_INPUT_RATE = 16000
DEFAULT_OUTPUT_RATE = 24000
SAMPLE_RATE_MIN = 8000
SAMPLE_RATE_MAX = 48000


class NetworkMicStream:
    """A ``sounddevice.InputStream``-shaped shim over a queue of inbound PCM.

    The WebSocket handler calls :meth:`feed` with raw PCM16 bytes as they arrive; the VAD worker
    calls :meth:`read` for exact-size int16 blocks. A sample split across two feeds survives via a
    one-byte carry, so a client that frames on arbitrary byte counts never loses audio.
    """

    def __init__(self, np: Any, *, stop: threading.Event, poll_seconds: float = 0.1) -> None:
        self._np = np
        self._stop = stop
        self._poll_seconds = poll_seconds
        self._chunks: queue.Queue[Optional[Any]] = queue.Queue()
        self._carry = np.zeros(0, dtype=np.int16)
        self._byte_carry = b""
        self._feed_lock = threading.Lock()
        self._epoch = 0
        self._read_epoch = 0

    def feed(self, pcm_bytes: bytes) -> None:
        if not pcm_bytes:
            return
        with self._feed_lock:
            buf = self._byte_carry + pcm_bytes
            if len(buf) % 2:
                buf, self._byte_carry = buf[:-1], buf[-1:]
            else:
                self._byte_carry = b""
            if buf:
                self._chunks.put((self._epoch, self._np.frombuffer(buf, dtype=self._np.int16).copy()))

    def discard(self) -> None:
        """Fence already received audio so control-phase samples cannot be replayed as input."""
        with self._feed_lock:
            self._epoch += 1
            self._byte_carry = b""
            while True:
                try:
                    self._chunks.get_nowait()
                except queue.Empty:
                    break

    def close(self) -> None:
        """Unblock any reader waiting for more samples."""
        self._stop.set()
        self._chunks.put(None)

    def read(self, block: int) -> tuple[Any, bool]:
        """Exactly *block* int16 samples as ``(np.ndarray, overflow=False)``. Blocks until enough
        samples arrive or the stop event is set; on stop, returns what is buffered zero-padded so
        the endpointer drains and exits cleanly instead of raising."""
        np = self._np
        if self._read_epoch != self._epoch:
            self._carry = np.zeros(0, dtype=np.int16)
            self._read_epoch = self._epoch
        while len(self._carry) < block:
            if self._stop.is_set():
                self._drain_pending()
                if len(self._carry) >= block:
                    break
                pad = np.zeros(block - len(self._carry), dtype=np.int16)
                out = np.concatenate([self._carry, pad])
                self._carry = np.zeros(0, dtype=np.int16)
                return out, False
            try:
                chunk = self._chunks.get(timeout=self._poll_seconds)
            except queue.Empty:
                continue
            if chunk is None:  # close() sentinel
                continue
            epoch, chunk = chunk
            if self._read_epoch != self._epoch:
                self._carry = np.zeros(0, dtype=np.int16)
                self._read_epoch = self._epoch
            if epoch == self._epoch:
                self._carry = np.concatenate([self._carry, chunk])
        out, self._carry = self._carry[:block], self._carry[block:]
        return out, False

    def _drain_pending(self) -> None:
        while True:
            try:
                chunk = self._chunks.get_nowait()
            except queue.Empty:
                return
            if chunk is not None:
                epoch, chunk = chunk
                if epoch == self._epoch:
                    self._carry = self._np.concatenate([self._carry, chunk])


class InputReady:
    """Wake the dispatcher after an empty or abandoned capture."""


class ConversationOutput:
    """A completed conversation response, not a synthetic user transcript."""

    def __init__(self, text: str, event_id: str, *, next_action: str = "standby") -> None:
        self.text, self.event_id, self.next_action = text, event_id, next_action
        self.expires_at = float("inf")
        self.on_dequeue: Callable[[], None] = lambda: None


class QuietTick:
    """Session-mode marker: the RECEIVED audio stream has been silent for another
    ``quiet_interval``; ``quiet_seconds`` is the cumulative received-silence since the last speech."""

    __slots__ = ("quiet_seconds",)

    def __init__(self, quiet_seconds: float) -> None:
        self.quiet_seconds = quiet_seconds


class TailBarge:
    """Marker: speech tripped DURING the playback tail (after turn_done, before the client's
    `drained`). The driver emits ``interrupted``; accepted user speech is captured separately."""

    __slots__ = ("turn_id",)

    def __init__(self, turn_id: Optional[str]) -> None:
        self.turn_id = turn_id


class CancelTurn:
    """Marker: the client sent ``{"type": "cancel"}``. The current capture is aborted (its
    transcript is discarded, no turn runs on it) and any in-flight turn's TTS + agent are stopped,
    but the socket, session and conversation history stay alive. The driver turns this into
    ``interrupted`` + ``turn_done`` for the cancelled turn."""

    __slots__ = ("turn_id",)

    def __init__(self, turn_id: Optional[str] = None) -> None:
        self.turn_id = turn_id or uuid.uuid4().hex


class Utterance(str):
    """A transcript carrying the tracing recorder of its audio capture."""

    def __new__(cls, text: str, recorder: Any) -> "Utterance":
        self = super().__new__(cls, text)
        self.recorder = recorder
        self.superseded = False
        return self


def _default_transcribe(wav_path: str) -> str:
    return speech.transcribe_wav(wav_path)


def _session_state(method):
    """Serialize short state transitions shared by the socket loop and VAD thread."""
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._state_lock:
            return method(self, *args, **kwargs)
    return locked


class ConverseSession:
    """Drives the VAD → STT loop against a :class:`NetworkMicStream` on a worker thread.

    ``transcribe(wav_path) -> str`` is the STT seam (default: the configured provider through
    :func:`speech.transcribe_wav`). The driver flips :meth:`set_playing` around TTS playback so the
    detector rejects speaker bleed; accepted speech interrupts the old reply and starts the next capture.
    """

    def __init__(
        self, np: Any, *, input_rate: int = DEFAULT_INPUT_RATE, quiet_interval: float = 0.0,
        transcribe: Optional[Callable[[str], str]] = None, barge_multiplier: Optional[float] = None,
        endpoint_model: bool = False, endpoint_threshold: float = SMART_TURN_DEFAULT_THRESHOLD,
        vad_model: str = DEFAULT_VAD_MODEL,
    ) -> None:
        self._np = np
        self._state_lock = threading.RLock()
        self.session_id = uuid.uuid4().hex  # echoed in `ready`, joinable across client/server traces
        self._transcribe = transcribe or _default_transcribe
        # Whisper resamples internally, so STT works at any capture rate; 30 ms blocks at that rate.
        self._input_rate = int(input_rate)
        # Session mode: quiet is measured in RECEIVED-audio silence, NOT wall clock. When no audio
        # arrives the worker parks in stream.read() and this clock does not advance, so a client
        # holding the socket open (streaming only after a wake word) never accrues phantom quiet.
        self._quiet_interval = float(quiet_interval)
        self._quiet_seconds = 0.0
        self._next_quiet_at = self._quiet_interval
        # Quiet is NOT accrued during a turn (the user has no window to speak into).
        self._turn_active = threading.Event()
        self._pending_turn = threading.Event()
        self._managed = False
        self._generation = 0
        self._active_turn_id: Optional[str] = None
        self._interrupt_callback: Optional[Callable[[], None]] = None
        self.on_tail_barge: Optional[Callable[[Optional[str]], None]] = None
        self._tail_drained = threading.Event()
        self._tail_drained.set()
        self._tail_interrupted = False
        # Playback tail: between turn_done and the client's `drained` (or a fallback) the client is
        # still PLAYING the reply; quiet is suppressed and onset must pass the echo guard.
        self._tail_active = False
        self._tail_turn_id: Optional[str] = None
        self._tail_deadline = 0.0
        self._stop = threading.Event()
        self._playing = threading.Event()
        self._tts_stop: Optional[threading.Event] = None
        self._interrupted = threading.Event()
        # Cancel: set by ``cancel()`` to abort an in-progress capture (its transcript is discarded,
        # no turn runs on it) and to stop an in-flight turn's TTS + agent without ending the session.
        self._cancelled = threading.Event()
        # True while the VAD worker is mid-capture, so ``cancel()`` knows whether aborting a capture
        # (and queuing a CancelTurn marker) applies to the capture happening right now.
        self._capturing = False
        self._continuation_capture = False
        self._queued_utterance: Optional[Utterance] = None
        self._active_utterance: Optional[Utterance] = None
        self.stream = NetworkMicStream(np, stop=self._stop)
        # The event stream the driver consumes: a transcript str, a QuietTick, a TailBarge, or the
        # None sentinel on shutdown.
        self.transcripts: queue.Queue = queue.Queue()
        self._block = int(self._input_rate * vad.BLOCK_MS / 1000)
        self._detector = SpeechDetector(np, model=vad_model, input_rate=self._input_rate, mult=barge_multiplier)
        self._pre_roll: deque = deque(maxlen=max(1, vad.PRE_ROLL_MS // vad.BLOCK_MS))
        self._endpoint_blocks = max(1, vad.ENDPOINT_SILENCE_MS // vad.BLOCK_MS)
        self._max_blocks = max(1, vad.MAX_UTTERANCE_MS // vad.BLOCK_MS)
        self._endpoint_threshold = float(endpoint_threshold)
        self._turn_detector: Any = None
        if endpoint_model:
            try:
                from .turn_detector import load_turn_detector

                self._turn_detector = load_turn_detector(self._endpoint_threshold)
            except Exception:
                logger.warning("smart-turn endpointer failed to load; using the fixed endpoint", exc_info=True)
                self._turn_detector = None
        self._worker: Optional[threading.Thread] = None
        self.on_trip: Optional[Callable[[str], None]] = None  # called with the phase name on every trip

    # ── playback / barge-in coordination ──
    def playing(self) -> bool:
        return self._playing.is_set()

    @_session_state
    def set_playing(self, value: bool, *, tts_stop: Optional[threading.Event] = None) -> None:
        """Mark playback active/idle; while active a VAD trip cuts *tts_stop*."""
        if tts_stop is not None:
            self._tts_stop = tts_stop
        elif not self._turn_active.is_set():
            self._tts_stop = None
        if value:
            self._interrupted.clear()
            self._playing.set()
        else:
            self._playing.clear()
            if not self._turn_active.is_set():
                self._tts_stop = None

    @_session_state
    def take_interrupted(self) -> bool:
        """Pop the barge-in flag; True when a trip cut playback since the last check."""
        if self._interrupted.is_set():
            self._interrupted.clear()
            return True
        return False

    def stop(self) -> None:
        """End the loop and unblock the reader and any transcript waiter."""
        self._stop.set()
        self.stream.close()
        self._tail_drained.set()
        self.transcripts.put(None)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def commit(self) -> None:
        """Force the current utterance to endpoint now (client sent ``commit``): feed enough zero
        blocks to satisfy the endpoint-silence window so capture returns promptly."""
        silence = self._np.zeros(self._block, dtype=self._np.int16).tobytes()
        for _ in range(self._endpoint_blocks + 1):
            self.stream.feed(silence)

    # ── worker loop ──
    def start(self) -> None:
        # STT reads provider credentials through the profile scope; a bare Thread would drop the
        # spawner's contextvars (the request's scope) under multiplexing.
        self._worker = speech.spawn_context_thread(self._run, name="converse-vad")
        self._worker.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if self._tail_active and time.monotonic() >= self._tail_deadline:
                    self.end_playback_tail()  # client never sent `drained`: don't stay suppressed forever
                data, _ = self.stream.read(self._block)
                if self._stop.is_set():
                    break
                with self._state_lock:
                    if self.stream._read_epoch != self.stream._epoch:
                        continue  # samples received before the last drain/reset
                    busy = self._turn_active.is_set() or self._pending_turn.is_set() or self._tail_active
                    playing = self.playing() or self._tail_active
                    phase = self._detector.feed(data, playing, interrupting=busy)
                    if phase is not None:
                        reply_phase = ("drain" if self._tail_active else "speaking" if playing
                                       else "thinking" if self._turn_active.is_set()
                                       else "pending" if busy else "idle")
                        logger.info("converse onset: session=%s turn=%s detector=%s reply_phase=%s probability=%s",
                                    self.session_id, self._active_turn_id, self._detector.name, reply_phase,
                                    getattr(self._detector, 'probability', None))
                    # Onset filtering rejects echo/noise; accepted speech always owns a capture.
                    self._pre_roll.append(data.copy())
                    if phase is None:
                        self._account_received_silence()
                        continue
                    self._reset_quiet()
                    if self.on_trip is not None:
                        try:
                            self.on_trip(phase)
                        except Exception:
                            logger.debug("converse on_trip callback failed", exc_info=True)
                    self._continuation_capture = busy
                    for utterance in (self._queued_utterance, self._active_utterance):
                        if utterance is not None:
                            utterance.superseded = True
                    self._capturing = True
                    if self._managed:
                        self._pending_turn.set()
                    self._cancelled.clear()
                    # Latch capture ownership BEFORE cancellation can finalize the old turn.
                    if self._tail_active:
                        self._interrupt_tail()
                        self.end_playback_tail()
                    elif self._turn_active.is_set():
                        self._trigger_barge_in()
                    capture_generation = self._generation
                    recorder = PhaseRecorder(input_rate=self._input_rate, session_mode=self._quiet_interval > 0,
                                             **{"session.id": self.session_id, "input.continuation": busy})
                try:
                    transcript, _volume = self._capture_and_transcribe(recorder)
                except BaseException:
                    with self._state_lock:
                        self._capturing = self._continuation_capture = False
                        self.release_capture()
                    raise
                with self._state_lock:
                    abandoned = self._capture_abandoned(capture_generation)
                    self._capturing = self._continuation_capture = False
                    if abandoned:
                        self._cancelled.clear()
                        recorder.set(outcome="cancelled")
                        emit_turn_trace(recorder)
                        self.release_capture()
                    elif transcript:
                        if self._managed:
                            self._pending_turn.set()
                        utterance = Utterance(transcript, recorder)
                        self._queued_utterance = utterance
                        self.transcripts.put(utterance)
                    else:
                        recorder.set(outcome="empty")
                        emit_turn_trace(recorder)
                        self.release_capture()
        except Exception:
            logger.warning("converse VAD loop failed", exc_info=True)
        finally:
            self.transcripts.put(None)

    @_session_state
    def _capture_abandoned(self, generation: int) -> bool:
        return (self._stop.is_set() or self._cancelled.is_set()
                or (not self._continuation_capture and (generation != self._generation
                    or self._turn_active.is_set() or self._tail_active)))

    def _account_received_silence(self) -> None:
        """One block of received non-speech audio elapsed. In session mode, accrue it and emit a
        :class:`QuietTick` each time the received silence crosses another quiet_interval."""
        if self._quiet_interval <= 0 or self._turn_active.is_set() or self._tail_active:
            return
        self._quiet_seconds += self._block / self._input_rate  # == 0.03 s per block
        if self._quiet_seconds + 1e-6 >= self._next_quiet_at:
            self.transcripts.put(QuietTick(round(self._quiet_seconds, 1)))
            self._next_quiet_at += self._quiet_interval

    def _reset_quiet(self) -> None:
        self._quiet_seconds = 0.0
        self._next_quiet_at = self._quiet_interval

    @_session_state
    def enable_turn_control(self) -> None:
        self._managed = True

    @_session_state
    def release_capture(self) -> None:
        self._pending_turn.clear()
        if self._managed:
            self.transcripts.put(InputReady())

    @_session_state
    def capture_pending(self) -> bool:
        return self._pending_turn.is_set() or self._capturing

    @_session_state
    def reserve_turn(self, turn_id: str, tts_stop: threading.Event, utterance=None) -> None:
        cancelled = (getattr(utterance, "superseded", False)
                     or (self._pending_turn.is_set() and self._cancelled.is_set()))
        if self._queued_utterance is utterance:
            self._queued_utterance = None
        self._active_utterance = utterance
        self.begin_turn(turn_id, tts_stop=tts_stop)
        if cancelled:
            self._trigger_barge_in()

    @_session_state
    def begin_turn(self, turn_id: Optional[str] = None, *, tts_stop=None, on_interrupt=None) -> None:
        """Bind cancellation from thinking through playback to this exact turn."""
        if self._active_turn_id != turn_id or not self._turn_active.is_set():
            self._generation += 1
        self._active_turn_id = turn_id
        self._tts_stop = tts_stop
        self._interrupt_callback = on_interrupt
        self._turn_active.set()
        self._pending_turn.clear()
        self._reset_quiet()

    @_session_state
    def end_turn(self) -> None:
        if not self._tail_active:
            self._active_turn_id = None
        self._turn_active.clear()
        self._active_utterance = None
        self._tts_stop = None
        self._interrupt_callback = None
        self._reset_quiet()

    @_session_state
    def finish_turn(self, turn_id: str, playback_tail_seconds: float) -> bool:
        # A continuation owns incoming PCM even if cancellation races with completion.
        continuation_pending = self.capture_pending()
        if playback_tail_seconds > 0 and not continuation_pending:
            self.begin_playback_tail(turn_id, playback_tail_seconds)
        self.end_turn()
        return continuation_pending

    @_session_state
    def begin_playback_tail(self, turn_id: Optional[str], fallback_seconds: float) -> None:
        """Driver hook: the reply is fully SENT but the client is still PLAYING it."""
        if fallback_seconds <= 0:
            return
        self._tail_drained.clear()
        self._tail_interrupted = False
        self._tail_active = True
        self._tail_turn_id = turn_id
        self._active_turn_id = turn_id
        self._tail_deadline = time.monotonic() + fallback_seconds

    @_session_state
    def end_playback_tail(self) -> None:
        self._tail_active = False
        self._tail_turn_id = None
        if not self.capture_pending():
            self.stream.discard()
            self._pre_roll.clear()
        self._tail_drained.set()
        self._active_turn_id = None
        self._tts_stop = None
        self._reset_quiet()

    @_session_state
    def notify_drained(self, turn_id: Optional[str] = None) -> None:
        """Client hook (``{"type":"drained"}``): ends the playback tail for the matching turn (or
        the current tail when no id is given)."""
        if self._tail_active and (turn_id is None or turn_id == self._tail_turn_id):
            self.end_playback_tail()

    async def wait_for_playback_tail(self) -> None:
        if self._tail_active:
            remaining = max(0, self._tail_deadline - time.monotonic())
            await asyncio.get_running_loop().run_in_executor(None, self._tail_drained.wait, remaining)
            if self._tail_active:
                self.end_playback_tail()

    @_session_state
    def _interrupt_tail(self) -> None:
        if self._tail_interrupted:
            return
        self._tail_interrupted = True
        if self.on_tail_barge is not None:
            self.on_tail_barge(self._tail_turn_id)
        else:
            self.transcripts.put(TailBarge(self._tail_turn_id))

    @_session_state
    def cancel(self, turn_id: Optional[str] = None) -> bool:
        """Cancel the matching turn; a stale target never affects a newer turn."""
        current = self._tail_turn_id if self._tail_active else self._active_turn_id
        if turn_id is not None and turn_id != current:
            return False
        if self._capturing and self._continuation_capture:
            self._cancelled.set()
            self.commit()
        if self._tail_active:
            self._interrupt_tail()
            return True
        if self._turn_active.is_set() or self._tts_stop is not None:
            self._trigger_barge_in()
            return True
        self._cancelled.set()
        if self._capturing:
            self.commit()
            self.transcripts.put(CancelTurn(turn_id))
        return True

    @_session_state
    def _trigger_barge_in(self) -> None:
        """Cut the in-flight reply exactly once, including before its first audio."""
        if self._interrupted.is_set():
            return
        try:
            speech.mark_speech_interrupted()
        except Exception:
            logger.debug("mark_speech_interrupted failed", exc_info=True)
        if self._tts_stop is not None:
            self._tts_stop.set()
        self._playing.clear()
        self._interrupted.set()
        if self._interrupt_callback is not None:
            self._interrupt_callback()

    def _capture_and_transcribe(self, recorder: Optional[PhaseRecorder] = None) -> tuple[str, float]:
        """Endpoint the utterance from the pre-roll and return ``(transcript, volume)`` — ``volume``
        is the captured audio's RMS loudness (:func:`vad.utterance_volume`). Wake
        selection uses only client-reported signals and never gates this capture."""
        np = self._np
        generation = self._generation
        recorder = recorder or PhaseRecorder()
        with recorder.phase("voice.capture", **{
                "endpoint.silence_ms": self._endpoint_blocks * self._block * 1000 / self._input_rate,
        }) as cap:
            if self._turn_detector is not None:
                cap.set(**{"endpoint.mode": "smart_turn"})
                wav_path, volume = self._capture_adaptive(phase=cap)
            else:
                cap.set(**{"endpoint.mode": "fixed"})
                wav_path, volume = vad.capture_until_quiet(
                    self.stream, np, self._block, self._pre_roll,
                    endpoint_blocks=self._endpoint_blocks, max_blocks=self._max_blocks,
                    sample_rate=self._input_rate, is_quiet=self._detector.is_quiet)
            cap.set(**{"vad.model": self._detector.name})
            # Audio duration differs from elapsed capture time when activation audio is buffered.
            # Read only the WAV header; no audio or transcript is exported.
            with contextlib.suppress(OSError, wave.Error):
                with wave.open(wav_path, "rb") as wav:
                    cap.set(**{"input.audio_ms": wav.getnframes() * 1000 / wav.getframerate()})
        self._pre_roll.clear()  # capture drained the pre-roll into the WAV
        if self._capture_abandoned(generation):
            vad.unlink_quietly(wav_path)
            return "", volume
        with recorder.phase("voice.stt") as stt:
            try:
                transcript = str(self._transcribe(wav_path) or "").strip()
            except Exception:
                logger.warning("converse transcription raised", exc_info=True)
                transcript = ""
            finally:
                vad.unlink_quietly(wav_path)
            stt.set(**{"stt.transcript_chars": len(transcript), "stt.success": bool(transcript)})
        return transcript, volume

    def _capture_adaptive(self, *, phase: Any = None) -> tuple[str, float]:
        """Semantic endpoint: mirror the fixed-silence capture, but on reaching a candidate pause ask
        Smart Turn whether the utterance *sounds* finished; hold (keep listening) when it doesn't,
        bounded by ``MAX_ENDPOINT_HOLDS`` and the utterance cap. Returns ``(wav_path, volume)``."""
        np = self._np
        frames = list(self._pre_roll)
        quiet = 0
        holds = 0
        for _ in range(self._max_blocks):
            data, _ = self.stream.read(self._block)
            if self._stop.is_set():
                break
            frames.append(data.copy())
            if self._detector.is_quiet(data):
                quiet += 1
            else:
                quiet = 0
                holds = 0  # the user resumed talking — restore the full patience budget
            if quiet < self._endpoint_blocks:
                continue
            try:
                prob = self._turn_detector.turn_complete_probability(self._frames_to_f32_16k(frames))
            except Exception:
                logger.debug("smart-turn inference failed; committing on silence", exc_info=True)
                if phase is not None:
                    phase.event("endpoint.decision", **{"endpoint.decision": "commit",
                                "endpoint.reason": "inference_error"})
                break
            decision = "commit" if prob >= self._endpoint_threshold or holds + 1 >= MAX_ENDPOINT_HOLDS else "hold"
            if phase is not None:
                phase.event("endpoint.decision", **{"endpoint.probability": prob,
                            "endpoint.threshold": self._endpoint_threshold, "endpoint.decision": decision,
                            "endpoint.holds": holds,
                            "endpoint.reason": "threshold" if prob >= self._endpoint_threshold else "hold_budget"
                            if decision == "commit" else "incomplete"})
            if prob >= self._endpoint_threshold:
                vad.vad_log(f"smart-turn: p={prob:.3f} >= {self._endpoint_threshold:.2f} -> commit")
                break
            holds += 1
            if holds >= MAX_ENDPOINT_HOLDS:
                vad.vad_log(f"smart-turn: p={prob:.3f} held {holds}x -> commit (budget spent)")
                break
            vad.vad_log(f"smart-turn: p={prob:.3f} < {self._endpoint_threshold:.2f} -> hold "
                        f"(keep listening, {holds}/{MAX_ENDPOINT_HOLDS})")
            quiet = 0  # keep the mic open for another window
        audio = np.concatenate(frames, axis=0)
        return vad.write_wav(np, audio, sample_rate=self._input_rate), vad.utterance_volume(np, audio)

    def _frames_to_f32_16k(self, frames: list) -> Any:
        """Concatenate int16 capture blocks into a 16 kHz mono float32 array for Smart Turn."""
        np = self._np
        audio = np.concatenate(frames, axis=0).astype(np.float32) / 32768.0
        if self._input_rate != 16000 and audio.size:
            n_out = round(audio.size * 16000 / self._input_rate)
            if n_out > 0:
                audio = np.interp(np.linspace(0.0, 1.0, n_out, endpoint=False),
                                  np.linspace(0.0, 1.0, audio.size, endpoint=False), audio).astype(np.float32)
        return audio


# ── start frame ──

def clamp_sample_rate(raw: Any, default: int) -> int:
    """Coerce *raw* to an int sample rate in [8000, 48000]; *default* when unusable."""
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return max(SAMPLE_RATE_MIN, min(val, SAMPLE_RATE_MAX))


def parse_quiet_interval(raw: Any) -> float:
    """The SESSION-mode opt-in from the ``start`` frame: seconds of received quiet between turns
    before a ``{"type":"quiet"}`` advisory. Absent /
    invalid / ``<= 0`` → ``0.0`` = continuous mode, the always-listening behaviour."""
    if raw is None:
        return 0.0
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if val <= 0 else min(val, QUIET_INTERVAL_MAX)


def parse_start_config(frame: dict[str, Any], *, default_name: str = DEFAULT_NAME,
                       ) -> tuple[int, int, float, str, Optional[str]]:
    """``(input_rate, output_rate, quiet_interval, name, profile)`` from a ``start`` frame. All
    fields optional; rates are clamped to [8000, 48000]."""
    input_rate = clamp_sample_rate(frame.get("input_rate"), DEFAULT_INPUT_RATE)
    output_rate = clamp_sample_rate(frame.get("output_rate"), DEFAULT_OUTPUT_RATE)
    quiet_interval = parse_quiet_interval(frame.get("quiet_interval"))
    raw_name = frame.get("assistant_name", frame.get("name"))
    name = str(raw_name).strip() if raw_name is not None and str(raw_name).strip() else default_name
    profile = frame.get("profile") or None
    return input_rate, output_rate, quiet_interval, name, (str(profile).strip() if profile else None)


# Voice replies are spoken aloud — keep them short and speakable. Sent as the run's ``instructions``
# (the ephemeral system prompt) so spoken replies don't balloon to full chat length.
VOICE_PROMPT = (
    "You are in a live, low-latency voice conversation and your reply is spoken aloud. Speed "
    "matters: answer immediately with your first, most direct response — do not deliberate, "
    "plan, weigh options, or think out loud before answering. Keep it concise and conversational "
    "— at most 2-3 short sentences of plain spoken text, with no code blocks, markdown, lists, or "
    "URLs. If the request is unclear or you only caught fragments, ask one short clarifying "
    "question instead of guessing or rambling. "
    "This is a persistent conversation. It can send a spoken update later without a new user "
    "message or wake word while its voice device is connected. For requested timers, reminders, "
    "follow-ups, or monitoring, use the scheduling tools actually available to you; do not "
    "simulate a timer by waiting in this turn or promise work that has not been scheduled. "
    "When Heartbeat 2's pulse tool is available, use it to create, list, update, pause, resume, "
    "or cancel wakes and watchers in this conversation. Follow its tool schema and bundled "
    "usage instructions. For a user-requested one-time reminder, set owner_asked only when "
    "the user asked for it. Report the tool's receipt, including pending approval, timing, "
    "quiet-hour delays, or failure; confirm a timer only after successful creation. On a "
    "Heartbeat wake, use pulse respond with the appropriate outcome, notify decision, and "
    "summary. Speak useful authorized updates proactively; if no update is needed, honor "
    "the scheduler's silent-response protocol. Signing off ends this microphone activation, "
    "not the conversation or an existing schedule. Cancelling speech does not cancel a timer: "
    "use the scheduling tool to cancel it. A scheduled reminder does not by itself guarantee "
    "an alarm sound, synchronized playback, or delivery to a disconnected device. If a tool "
    "or delivery integration is unavailable, say so instead of claiming success."
)


def voice_system_prompt(name: Optional[str] = None, *, allow_signoff: bool = False,
                        end_phrases: tuple[str, ...] = DEFAULT_END_PHRASES,
                        device_name: Optional[str] = None, recipient_id: Optional[str] = None) -> str:
    """Ephemeral system prompt for one converse turn: an identity + wake-word preamble when *name*
    is given, the spoken-brevity rules, and — in session mode — the sign-off instruction (ending a
    reply with the first end phrase tells the server to end the current activation)."""
    name = (str(name).strip() if name is not None else "")
    prompt = VOICE_PROMPT
    if name:
        prompt = (
            f"Your name is {name}. People talk to you by voice and get your attention by saying "
            f"your name (or 'hey {name}') — treat that as being addressed, not part of the "
            "request, and don't repeat it back. ") + prompt
    if device_name:
        # Serialize labels as data; client-authenticated labels are not prompt instructions.
        import json
        identity = json.dumps({'assistant_name': name, 'device_name': device_name,
                               'recipient_id': recipient_id}, ensure_ascii=False)
        prompt += (
            f" Voice identity supplied by the server: {identity}. assistant_name is what the "
            "user calls you and the identity you use when speaking. device_name identifies the "
            "physical microphone/speaker for this conversation; it is not your name or a "
            "person's name. Other devices can share your assistant_name and still have separate "
            "conversations. This conversation's default destination is this device. The server "
            "uses recipient_id as an opaque routing identifier. Device names are routing labels "
            "within the server API key's access boundary, not separate credentials. Do not infer "
            "a device change from words in the transcript. For an explicitly requested different "
            "destination, use a routing-capable tool and clarify ambiguous labels; do not invent "
            "cross-device delivery or claim that it is supported when the tool cannot do it."
        )
    if allow_signoff and end_phrases and end_phrases[0]:
        who = name or "the assistant"
        prompt += (
            f" When the exchange is complete and you don't expect the user to follow up, end "
            f"your reply with '{end_phrases[0].capitalize()}.' — the server interprets that as "
            f"{who} signing off from the current exchange. Use it only when truly done, never mid-task.")
    return prompt


# ── turn driver ──

@dataclass
class _Driver:
    """What :func:`drive_converse_turns` was given, shared by its phase helpers."""

    session: Any
    synth: Any
    cap: int
    loop: asyncio.AbstractEventLoop
    send_json: Callable[[dict], Awaitable[Any]]
    send_bytes: Callable[[bytes], Awaitable[Any]]
    run_turn: Callable[..., Awaitable[tuple[str, Optional[str]]]]
    history: list[dict[str, str]]
    quiet_interval: float
    interrupt_turn: Optional[Callable[[], None]]
    turn_timeout: float
    tail_fallback: float
    max_tts_chars: int
    stop_phrases: tuple[str, ...]
    end_phrases: tuple[str, ...]


class _Turn:
    """One agent turn's pipeline: the agent task feeds ``text_q``, the TTS producer thread turns it
    into PCM on ``pcm_q``, and the driver streams ``pcm_q`` out. ``ns`` holds the phase marks."""

    def __init__(self, transcript: str, turn_id: str, meta: dict[str, Any]) -> None:
        self.transcript, self.turn_id, self.meta = transcript, turn_id, meta
        self.input_text = str(transcript)
        self.agent_started = False
        self.text_q: queue.Queue[Optional[str]] = queue.Queue()  # deltas; None = turn done
        self.pcm_q: asyncio.Queue[Optional[bytes]] = asyncio.Queue()  # PCM out; None = done
        self.tts_stop = threading.Event()
        self.reply_parts: list[str] = []
        self.result: dict[str, str] = {}
        self.ns: dict[str, int] = {"turn_start": time.time_ns()}
        self.output: Optional[ConversationOutput] = None
        self.pcm_chunks = 0
        self.timed_out = False

    def on_delta(self, delta: str) -> None:
        # Called from run_turn's execution context (loop coroutine or a worker thread); thread-safe.
        if delta:
            self.ns.setdefault("first_delta", time.time_ns())
            self.reply_parts.append(delta)
            self.text_q.put(delta)

    def ms(self, key: str, since: str = "turn_start") -> Optional[int]:
        ns = self.ns
        return round((ns[key] - ns[since]) / 1e6) if key in ns and since in ns else None


async def drive_converse_turns(
    *,
    session: Any,
    synth: Any,
    cap: int,
    loop: asyncio.AbstractEventLoop,
    send_json: Callable[[dict], Awaitable[Any]],
    send_bytes: Callable[[bytes], Awaitable[Any]],
    run_turn: Callable[..., Awaitable[tuple[str, Optional[str]]]],
    history: list[dict[str, str]],
    quiet_interval: float = 0.0,
    interrupt_turn: Optional[Callable[[], None]] = None,
    turn_timeout: float = DEFAULT_TURN_TIMEOUT,
    tail_fallback: float = DEFAULT_TAIL_FALLBACK,
    max_tts_chars: int = DEFAULT_MAX_TTS_CHARS,
    stop_phrases: tuple[str, ...] = DEFAULT_STOP_PHRASES,
    end_phrases: tuple[str, ...] = DEFAULT_END_PHRASES,
) -> None:
    """Run the per-transcript incremental-TTS turn loop.

    One turn per transcript pulled off ``session.transcripts``: announce it (``transcript``), run
    the agent turn via ``run_turn(transcript, on_delta, *, interrupted) -> (reply_text, err)``
    (deltas stream out as they land), and speak the reply INCREMENTALLY — each sentence is
    synthesized and sent the moment it is ready. Control-frame order per turn:
    ``transcript`` → ``thinking`` → (``speaking`` + PCM frames) → optional ``interrupted``/``error``
    → ``turn_done``.

    BARGE-IN: a VAD trip while playing stops TTS playback (``session`` sets ``tts_stop``), we emit
    ``interrupted``, and — when the host provides ``interrupt_turn`` — the in-flight agent turn is
    cancelled so it stops generating / firing tools.

    """
    d = _Driver(session=session, synth=synth, cap=cap, loop=loop, send_json=send_json,
                send_bytes=send_bytes, run_turn=run_turn, history=history,
                quiet_interval=quiet_interval, interrupt_turn=interrupt_turn,
                turn_timeout=turn_timeout, tail_fallback=tail_fallback, max_tts_chars=max_tts_chars,
                stop_phrases=tuple(stop_phrases), end_phrases=tuple(end_phrases))
    session.enable_turn_control()
    async def notify_tail_barge(turn_id):
        await send_json({"type": "interrupted", "turn_id": turn_id})
        await send_json({"type": "turn_done", "turn_id": turn_id, "next_action": "listen"})

    def tail_barge(turn_id):
        async def notify():
            try:
                await notify_tail_barge(turn_id)
            except Exception:
                logger.debug("tail interrupt delivery failed", exc_info=True)
        loop.call_soon_threadsafe(lambda: asyncio.create_task(notify()))
    session.on_tail_barge = tail_barge
    deferred_outputs = deque()
    unsubmitted = []
    while not session.stopped:
        if deferred_outputs and not session.capture_pending():
            item = deferred_outputs.popleft()
        else:
            item = await loop.run_in_executor(None, session.transcripts.get)
        if item is None:  # shutdown sentinel
            break
        if isinstance(item, InputReady):
            continue
        if isinstance(item, QuietTick):
            await send_json({"type": "quiet", "quiet_seconds": item.quiet_seconds})
            continue
        if isinstance(item, TailBarge):
            await notify_tail_barge(item.turn_id)
            continue
        if isinstance(item, CancelTurn):
            unsubmitted.clear()
            # A cancelled capture / turn: the socket, session and history stay alive; only this
            # turn stops. Emit interrupted + turn_done for it.
            await send_json({"type": "interrupted", "turn_id": item.turn_id})
            await send_json({"type": "turn_done", "turn_id": item.turn_id, "next_action": "listen"})
            continue
        if isinstance(item, ConversationOutput):
            if session.capture_pending():
                deferred_outputs.append(item)
                continue
            item.on_dequeue()
            if item.expires_at <= time.time():
                continue
            turn_id = uuid.uuid4().hex
            tt = start_turn(recorder=None, turn_id=turn_id,
                            **{"session.id": getattr(session, "session_id", None)})
            turn = _Turn("", turn_id, {"turn_id": turn_id, "event_id": item.event_id})
            turn.output = item
            if tt.traceparent:
                turn.meta["traceparent"] = tt.traceparent
            try:
                await _run_turn(d, turn, tt)
                await session.wait_for_playback_tail()
            except asyncio.CancelledError:
                tt.set(outcome="interrupted", interrupted=True)
                raise
            except Exception:
                tt.set(outcome="error")
                raise
            finally:
                tt.end()
            continue
        transcript = item
        if not transcript:
            continue
        turn_id = uuid.uuid4().hex
        tt = start_turn(recorder=getattr(item, "recorder", None), turn_id=turn_id,
                        **{"session.id": getattr(session, "session_id", None)})
        meta: dict[str, Any] = {"turn_id": turn_id}
        if tt.traceparent:
            meta["traceparent"] = tt.traceparent
        turn = _Turn(transcript, turn_id, meta)
        turn.input_text = "\n".join([*unsubmitted, str(transcript)])
        stop_word = is_stop_phrase(transcript, d.stop_phrases)
        try:
            if not stop_word:
                session.reserve_turn(turn_id, turn.tts_stop, transcript)
            await send_json({"type": "transcript", "text": transcript, **meta})
            if stop_word:
                unsubmitted.clear()
                await send_json({"type": "stop_word", "text": transcript, "turn_id": turn_id})
                tt.set(outcome="stop_word")
                session.release_capture()
                continue
            action = await _run_turn(d, turn, tt)
            if turn.agent_started:
                unsubmitted.clear()
            elif getattr(transcript, "superseded", False):
                unsubmitted.append(str(transcript))
            else:
                unsubmitted.clear()  # explicit cancellation does not carry a request forward
            if action == "standby":
                _discard_after_signoff(session, deferred_outputs)
            await session.wait_for_playback_tail()
        except asyncio.CancelledError:
            tt.set(outcome="interrupted", interrupted=True)
            raise
        except Exception:
            tt.set(outcome="error")
            raise
        finally:
            tt.end()


async def _run_turn(d: _Driver, turn: _Turn, tt: Any) -> str:
    """Run one agent turn and speak its reply; return the explicit client next action."""
    session = d.session
    interrupted_in = session.take_interrupted()
    if turn.tts_stop.is_set():
        async def cancelled_reply():
            turn.text_q.put(None)
        turn_task = asyncio.ensure_future(cancelled_reply())
    elif turn.output is not None:
        async def prepared_reply():
            turn.on_delta(turn.output.text)
            turn.result["reply"] = turn.output.text
            turn.ns["agent_end"] = time.time_ns()
            turn.text_q.put(None)
        turn_task = asyncio.ensure_future(prepared_reply())
    else:
        turn_task = asyncio.ensure_future(_run_agent(d, turn, interrupted_in))
    def interrupt():
        d.loop.call_soon_threadsafe(turn_task.cancel)
    session.begin_turn(turn.turn_id, tts_stop=turn.tts_stop, on_interrupt=interrupt)
    progress_task = None
    try:
        if not turn.tts_stop.is_set():
            await d.send_json({"type": "thinking", **turn.meta})
        # The producer carries the request's credential/profile context.
        speech.spawn_context_thread(_produce_speech, name="converse-tts", args=(d, turn)).start()
        progress_task = asyncio.create_task(_report_working(d, turn))
        await _stream_reply(d, turn, turn_task)

        # Barge-in: the VAD worker set `tts_stop` and the audio is already cut. Cancel the IN-FLIGHT
        # agent turn via the host's hook so `await turn_task` below returns at once instead of
        # blocking on a run that keeps going. (timed_out sets tts_stop too; that path already cancelled.)
        if turn.tts_stop.is_set() and not turn.timed_out and d.interrupt_turn is not None:
            try:
                d.interrupt_turn()
            except Exception:
                logger.debug("converse: interrupt_turn failed", exc_info=True)

        logger.info(
            "converse timing: first_delta=%sms first_sentence=%sms first_pcm=%sms total=%sms "
            "deltas=%d reply_chars=%d",
            turn.ms("first_delta"), turn.ms("first_sentence"), turn.ms("first_pcm"),
            turn.ms("pcm_done"), len(turn.reply_parts), sum(len(p) for p in turn.reply_parts))

        try:
            await turn_task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise  # the driver itself is being cancelled, not the (timed-out) turn task
        except Exception:
            pass  # recorded in turn.result by _run_agent
    except (asyncio.CancelledError, Exception):
        # The host abandoned the turn (the socket closed): end synthesis and cancel the agent task,
        # whose cancellation stops the run on the server.
        turn.tts_stop.set()
        if not turn_task.done():
            turn_task.cancel()
            with contextlib.suppress(BaseException):
                await turn_task
        raise
    finally:
        # Join before terminal frames, including cancellation/disconnect paths: no stale
        # keepalives may extend a client's next turn or outlive this driver.
        if progress_task is not None:
            progress_task.cancel()
            await asyncio.gather(progress_task, return_exceptions=True)

    if turn.output:
        reply = turn.result.get("reply") or "".join(turn.reply_parts)
    else:
        reply = _record_history(d.history, turn)
    barged = bool(session.take_interrupted() or turn.tts_stop.is_set()) and not turn.timed_out
    turn_err = bool(turn.result.get("err"))
    if barged:
        await d.send_json({"type": "interrupted", "turn_id": turn.turn_id})
    elif turn_err:
        await d.send_json({"type": "error", "error": turn.result["err"], "turn_id": turn.turn_id})
    # Only a completed reply may sign off. Partial output from a failed or interrupted
    # run is not a decision to put the client in standby.
    completed = not (barged or turn_err or turn.timed_out)
    next_action = (
        "standby" if completed and d.quiet_interval > 0 and is_end_phrase(reply, d.end_phrases)
        else "listen"
    )
    if completed and turn.output:
        next_action = turn.output.next_action
    # Arm before turn_done so an immediate drained acknowledgement cannot be lost.
    if session.finish_turn(turn.turn_id, d.tail_fallback if turn.pcm_chunks else 0):
        next_action = "listen"
    turn_done: dict[str, Any] = {"type": "turn_done", "turn_id": turn.turn_id,
                                "next_action": next_action}
    await d.send_json(turn_done)
    _finish_trace(tt, turn, reply, err=turn_err, barged=barged, next_action=next_action)
    return next_action


async def _report_working(d: _Driver, turn: _Turn) -> None:
    """Turn liveness, not proof of model/tool progress; never extends the server deadline."""
    while True:
        await asyncio.sleep(WORKING_FRAME_INTERVAL)
        if turn.tts_stop.is_set() or d.session.stopped:
            return
        await d.send_json({"type": "working", **turn.meta})


async def _run_agent(d: _Driver, turn: _Turn, interrupted: bool) -> None:
    """Drive one agent turn via the host adapter; the None sentinel closes the synthesis pipeline
    when the turn ends (whichever way it ends)."""
    try:
        turn.agent_started = True
        reply, err = await d.run_turn(turn.input_text, turn.on_delta, interrupted=interrupted)
        turn.result["reply"] = reply
        if err:
            turn.result["err"] = err
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("converse: voice turn failed", exc_info=True)
        turn.result["err"] = f"voice turn failed: {exc}"
    finally:
        turn.ns.setdefault("agent_end", time.time_ns())
        turn.text_q.put(None)


def _reply_sentences(d: _Driver, turn: _Turn) -> Iterator[str]:
    """Cut the streaming deltas into sentences as they land. A buffered tail is spoken after ~2 s
    without a new delta (at once if it already ends a sentence), unless it is an open <think>."""
    chunker = speech.sentence_chunker()
    idle_polls = 0
    while not (turn.tts_stop.is_set() or d.session.stopped):
        try:
            delta = turn.text_q.get(timeout=IDLE_POLL_SECONDS)
        except queue.Empty:
            idle_polls += 1
            buffered = chunker.buf.strip()
            if not buffered or ("<think" in chunker.buf and "</think>" not in chunker.buf):
                continue
            if buffered.endswith((".", "!", "?", "…", ":")) or idle_polls >= IDLE_POLLS_TO_FLUSH:
                yield from chunker.flush()
            continue
        idle_polls = 0
        if delta is None:
            yield from chunker.flush()
            return
        yield from chunker.feed(delta)


def _produce_speech(d: _Driver, turn: _Turn) -> None:
    """TTS producer thread: synthesize each sentence as it lands, so playback overlaps generation."""
    spoken_chars = 0
    try:
        for sentence in _reply_sentences(d, turn):
            cleaned = speech.strip_markdown(sentence)
            # The sign-off phrase is a control marker, not something to say aloud.
            if d.quiet_interval > 0:
                cleaned = strip_end_phrase(cleaned, d.end_phrases)
            if not cleaned:
                continue
            turn.ns.setdefault("first_sentence", time.time_ns())
            for piece in split_text_for_tts_stream(cleaned, d.cap):
                for chunk in d.synth.synth(piece):
                    if turn.tts_stop.is_set() or d.session.stopped:
                        return
                    turn.ns.setdefault("first_pcm", time.time_ns())
                    d.loop.call_soon_threadsafe(turn.pcm_q.put_nowait, chunk)
            spoken_chars += len(cleaned)
            if spoken_chars >= d.max_tts_chars:
                logger.debug("converse: TTS output capped at %d chars", spoken_chars)
                break
    except Exception as exc:
        logger.warning("converse synthesis failed: %s", exc, exc_info=True)
    finally:
        d.loop.call_soon_threadsafe(turn.pcm_q.put_nowait, None)


async def _stream_reply(d: _Driver, turn: _Turn, turn_task: asyncio.Future) -> None:
    """Stream the reply's PCM out; flip `playing` on only when real audio starts. A turn deadline
    backstops a hung run: once it trips the turn is abandoned, the task cancelled, and an error +
    turn_done still go out, so the client is never left "thinking" forever."""
    speaking = False
    deadline = d.loop.time() + d.turn_timeout if d.turn_timeout > 0 else None
    while True:
        try:
            if deadline is None:
                chunk = await turn.pcm_q.get()
            else:
                remaining = deadline - d.loop.time()
                if remaining <= 0:
                    turn.timed_out = True
                    break
                chunk = await asyncio.wait_for(turn.pcm_q.get(), timeout=remaining)
        except TimeoutError:
            turn.timed_out = True
            break
        if chunk is None or turn.tts_stop.is_set():
            break
        if not speaking:
            d.session.set_playing(True, tts_stop=turn.tts_stop)
            if turn.tts_stop.is_set():
                d.session.set_playing(False)
                break
            await d.send_json({"type": "speaking", **turn.meta})
            speaking = True
        if turn.tts_stop.is_set():
            break
        turn.pcm_chunks += 1
        await d.send_bytes(chunk)
    turn.ns["pcm_done"] = time.time_ns()
    if turn.timed_out:
        turn.tts_stop.set()
        turn_task.cancel()
        turn.result.setdefault("err", f"voice turn timed out after {d.turn_timeout:.0f}s")
        logger.warning("converse: turn abandoned after %.0fs (no completion)", d.turn_timeout)
    if speaking:
        d.session.set_playing(False)


def _record_history(history: list[dict[str, str]], turn: _Turn) -> str:
    """Append the turn to the socket's history (bounded tail); return the reply — the adapter's
    (the agent's final response), falling back to the streamed deltas."""
    reply = turn.result.get("reply") or "".join(turn.reply_parts)
    if not turn.agent_started:
        return reply
    history.append({"role": "user", "content": turn.input_text})
    if reply:
        history.append({"role": "assistant", "content": reply})
    if len(history) > HISTORY_MAX_MESSAGES:
        del history[:-HISTORY_MAX_MESSAGES]
    return reply


def _finish_trace(tt: Any, turn: _Turn, reply: str, *, err: bool, barged: bool,
                  next_action: str) -> None:
    tt.add_phase("voice.agent", turn.ns["turn_start"], turn.ns.get("agent_end"),
                 **{"llm.ttft_ms": turn.ms("first_delta"), "reply.chars": len(reply),
                    "llm.deltas": len(turn.reply_parts), "error": err, "llm.timed_out": turn.timed_out})
    tt.add_phase("voice.tts", turn.ns.get("first_sentence"), turn.ns["pcm_done"],
                 **{"tts.pcm_chunks": turn.pcm_chunks,
                    "tts.first_pcm_ms": turn.ms("first_pcm", since="first_sentence")})
    outcome = ("timeout" if turn.timed_out else "interrupted" if barged else "error" if err else "ok")
    tt.set(outcome=outcome, interrupted=barged, next_action=next_action)
    tt.end()


def drain_pending(session: Any, outputs: deque[ConversationOutput]) -> list[str]:
    """Discard every queued item on the session's transcripts queue, preserving the shutdown
    sentinel. Returns the dropped transcripts (ticks and other markers are discarded silently)."""
    dropped: list[str] = []
    q = session.transcripts
    while True:
        try:
            it = q.get_nowait()
        except queue.Empty:
            break
        if it is None:
            q.put(None)
            break
        if isinstance(it, ConversationOutput):
            outputs.append(it)
            continue
        if isinstance(it, str):
            dropped.append(it)
    return dropped


def _discard_after_signoff(session: Any, outputs: deque[ConversationOutput]) -> None:
    """When the agent signed off the client sleeps, so DROP any utterances that queued behind this
    turn — otherwise their replies arrive after the client has gone to sleep."""
    for dropped in drain_pending(session, outputs):
        recorder = getattr(dropped, "recorder", None)
        if recorder is not None:
            recorder.set(outcome="discarded_after_signoff")
            emit_turn_trace(recorder)
