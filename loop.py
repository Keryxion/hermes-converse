"""Framework-agnostic realtime voice loop: a network mic shim, the VAD→STT session task, and the
per-turn incremental-TTS driver. Nothing here touches a socket or a web framework, so it is unit
testable with numpy alone.

Pieces:

* :class:`NetworkMicStream` — inbound PCM from a WebSocket, awaited as 30 ms int16 blocks.
* :class:`ConverseSession` — drives the VAD/STT loop as a task: read 30 ms blocks, feed the
  :class:`~.speech_detector.SpeechDetector`, and on a trip capture the utterance, transcribe it and hand the
  transcript to the driver. It also owns the ``playing`` flag and barge-in (a trip while playing cuts
  TTS) and the session-mode quiet / playback-tail accounting.
* :func:`drive_converse_turns` — one agent turn per transcript: announce it, run the turn through
  the host's ``run_turn`` (deltas stream out as they land), speak the reply sentence by sentence.

Everything awaits on the route's event loop. CPU-bound audio work runs on the one audio thread every
connection shares (:func:`~.audio_transport.on_audio_thread`); STT and each TTS chunk are short
blocking calls through ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from functools import wraps
import contextlib
import logging
import math
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from . import speech, vad
from .audio_transport import on_audio_thread
from .echo import ReplyLevel, says_reply
from .level import InputLevel
from .speech_detector import DEFAULT_VAD_MODEL, SpeechDetector
from .settings import (
    DEFAULT_CAPTURE_NO_DATA_SECONDS, DEFAULT_ENDPOINT_MAX_SILENCE_MS,
    DEFAULT_ENDPOINT_SILENCE_MS, DEFAULT_MAX_TTS_CHARS, DEFAULT_TAIL_FALLBACK,
    DEFAULT_TURN_TIMEOUT)
from .synth import split_text_for_tts_stream
from .tracing import PhaseRecorder, emit_turn_trace, start_turn
from .voice_trace import BargeStats, begin_speech_interrupt

logger = logging.getLogger(__name__)

# After a granted wake the user often pauses to check the device is listening. Until this much speech
# follows onset, a pause ends that capture only at the max-silence allowance, so "Hey Arya ... set a
# timer" stays one request. A continuous "Hey Arya, set a timer" passes it before its final pause.
WAKE_SPEECH_MS = 1000
# Received-audio gap that marks the client stopping and resuming its stream. Live clients send
# frames every few tens of milliseconds; a longer network stall only delays the next quiet tick.
STREAM_RESUME_GAP_SECONDS = 1.0
# Reply audio is sent at most this far ahead of the client's playback position, so control frames
# (`interrupted`, `turn_done`, pongs) never queue behind a long backlog on the shared socket.
MAX_AUDIO_LEAD_SECONDS = 1.5
# Pacing waits in short steps so an interruption stops sending promptly.
AUDIO_PACE_STEP_SECONDS = 0.05
# After the approval question's paced audio ends, how long its echo may still reach the microphone.
APPROVAL_LISTEN_GRACE_SECONDS = 0.5
# How often a turn checks whether it is waiting on the person, for its `state` frames.
STATE_POLL_SECONDS = 0.1
# Reply speech: poll for deltas every 0.5 s; after 4 empty polls (~2 s) speak the buffered tail.
IDLE_POLL_SECONDS = 0.5
IDLE_POLLS_TO_FLUSH = 4
# Keep clients with shorter inactivity deadlines alive during agent/TTS waits.
THINKING_FRAME_INTERVAL = 5.0
SMART_TURN_DEFAULT_THRESHOLD = 0.5
# Bound on the per-connection conversation history kept in memory (~20 user+assistant turns): a
# simple tail-cap so a long-lived socket can't grow it without limit. The gateway session owns the real
# transcript; this copy backs the sign-off / next_action
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
    """A microphone fed by a WebSocket.

    The WebSocket handler calls :meth:`feed` with raw PCM16 bytes as they arrive; the session's task
    awaits exact-size int16 blocks from :meth:`read_blocks`. A sample split across two feeds survives
    via a one-byte carry, so a client that frames on arbitrary byte counts never loses audio.
    """

    def __init__(self, np: Any, *, stop: threading.Event) -> None:
        self._np = np
        self._stop = stop
        self._chunks: deque = deque()
        self._arrived = _Waiters()
        self._carry = np.zeros(0, dtype=np.int16)
        self._byte_carry = b""
        self._feed_lock = threading.Lock()
        self._epoch = 0
        self._read_epoch = 0
        self._last_feed = time.monotonic()
        self.no_data_return = False
        # A feed after this long without audio means the client stopped and resumed sending (for
        # example, stopped listening and started again); ``resumed`` flags it for the reader.
        self.resume_gap_seconds = STREAM_RESUME_GAP_SECONDS
        self.resumed = False
        self._pending_resume = False
        # Set when the reader reaches a client ``commit`` marker, which is queued in order with the
        # audio so a commit never overtakes the speech sent before it. ``commit_from`` is the index,
        # in the blocks the last read returned, of the first block holding the marker.
        self.committed = False
        self.commit_from: Optional[int] = None

    def feed(self, pcm_bytes: bytes, *, commit: bool = False) -> None:
        if not pcm_bytes:
            return
        with self._feed_lock:
            now = time.monotonic()
            # Held until a chunk is queued: a lone odd byte only fills the carry.
            self._pending_resume = self._pending_resume or now - self._last_feed >= self.resume_gap_seconds
            self._last_feed = now
            buf = self._byte_carry + pcm_bytes
            if len(buf) % 2:
                buf, self._byte_carry = buf[:-1], buf[-1:]
            else:
                self._byte_carry = b""
            if not buf:
                return
            self._chunks.append((self._epoch, self._np.frombuffer(buf, dtype=self._np.int16).copy(),
                                 self._pending_resume, commit))
            self._pending_resume = False
        self._arrived.wake()

    def mark_resumed(self) -> None:
        """The next audio received begins a new listening phase, as after a gap."""
        with self._feed_lock:
            self._pending_resume = True

    def discard(self) -> None:
        """Fence already received audio so control-phase samples cannot be replayed as input."""
        with self._feed_lock:
            self._epoch += 1
            self._byte_carry = b""
            self._pending_resume = False
            self.committed = False
            self._chunks.clear()

    def close(self) -> None:
        """Wake any reader waiting for more samples."""
        self._stop.set()
        self._arrived.wake()

    async def read_blocks(self, block: int, *, no_data_seconds: Optional[float] = None) -> list[Any]:
        """The whole *block*-sample int16 blocks received so far, waiting until there is at least
        one. On stop, returns what is buffered with the last block zero-padded, so the endpointer
        drains and exits cleanly instead of raising.

        A positive ``no_data_seconds`` bounds capture waits since the last feed. On timeout, return
        the partial block padded with zeros and set ``no_data_return``; the caller must drop its
        capture instead of treating the padding as received silence. Idle reads omit it.

        A commit marker ends the blocks returned, and so does a resumed stream, so each flag
        applies from the block where it arrived.
        """
        np = self._np
        self.no_data_return = False
        self.commit_from = None
        timeout = no_data_seconds if no_data_seconds is not None and no_data_seconds > 0 else None
        while True:
            with self._feed_lock:
                if self._read_epoch != self._epoch:
                    self._carry = np.zeros(0, dtype=np.int16)
                    self._read_epoch = self._epoch
                commit_at = self._drain_pending(block)
                if len(self._carry) >= block:
                    break
                if self._stop.is_set():
                    out = np.concatenate([self._carry, np.zeros(block - len(self._carry), dtype=np.int16)])
                    self._carry = np.zeros(0, dtype=np.int16)
                    return [out]
                remaining = None
                if timeout is not None:
                    remaining = self._last_feed + timeout - time.monotonic()
                    if remaining <= 0:
                        self.no_data_return = True
                        self._byte_carry = b""  # an abandoned half-sample cannot prefix the next stream
                        out = np.pad(self._carry, (0, block - len(self._carry)))
                        self._carry = np.zeros(0, dtype=np.int16)
                        return [out]
                arrival = self._arrived.add()
            try:
                await asyncio.wait({arrival}, timeout=remaining)
            finally:
                self._arrived.discard(arrival)
        count = len(self._carry) // block
        blocks = [self._carry[i * block:(i + 1) * block] for i in range(count)]
        self._carry = self._carry[count * block:]
        if commit_at is not None:
            self.commit_from = commit_at // block
        return blocks

    def unread(self, samples: Any) -> None:
        """Give back samples already read, so the next read returns them first."""
        if self._read_epoch == self._epoch:
            self._carry = self._np.concatenate([samples, self._carry])

    def ready_blocks(self, block: int) -> list[Any]:
        """Whole *block*-sample blocks already received, without waiting for more audio."""
        with self._feed_lock:
            if self._read_epoch != self._epoch:
                self._carry = self._np.zeros(0, dtype=self._np.int16)
                self._read_epoch = self._epoch
            self._drain_pending()
        blocks = []
        while len(self._carry) >= block:
            out, self._carry = self._carry[:block], self._carry[block:]
            blocks.append(out)
        return blocks

    def _drain_pending(self, block: Optional[int] = None) -> Optional[int]:
        """Move received chunks into the carry. Given *block*, stop after a commit marker, and
        before a resumed stream once a block is already waiting; return the carry offset where a
        commit marker starts."""
        commit_at = None
        while self._chunks:
            epoch, chunk, resumed, commit = self._chunks[0]
            if block is not None and resumed and epoch == self._epoch and len(self._carry) >= block:
                break
            self._chunks.popleft()
            if epoch != self._epoch:
                continue
            if commit:
                commit_at = len(self._carry)
            self._carry = self._np.concatenate([self._carry, chunk])
            self.resumed = self.resumed or resumed
            self.committed = self.committed or commit
            if block is not None and commit:
                break
        return commit_at


class _Waiters:
    """Coroutines waiting on something another task or a thread changes. A thread wakes them
    through their own loops, so a coroutine waits without holding a worker thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiting: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []

    def add(self) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with self._lock:
            self._waiting.append((loop, future))
        return future

    def discard(self, future: asyncio.Future) -> None:
        with self._lock:
            self._waiting = [w for w in self._waiting if w[1] is not future]

    def wake(self) -> None:
        with self._lock:
            waiting, self._waiting = self._waiting, []
        if not waiting:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        for loop, future in waiting:
            if loop is running:
                _resolve(future)
                continue
            with contextlib.suppress(RuntimeError):  # its loop has closed
                loop.call_soon_threadsafe(_resolve, future)


def _resolve(future: asyncio.Future) -> None:
    if not future.done():
        future.set_result(None)


class AsyncQueue(queue.Queue):
    """A queue that threads and tasks put to and a coroutine awaits."""

    def __init__(self) -> None:
        super().__init__()
        self._waiters = _Waiters()

    def _put(self, item: Any) -> None:
        super()._put(item)
        self._waiters.wake()

    async def get_async(self, timeout: Optional[float] = None) -> Any:
        """The next item; ``queue.Empty`` after *timeout* seconds without one."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with contextlib.suppress(queue.Empty):
                return self.get_nowait()
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise queue.Empty
            future = self._waiters.add()
            try:
                if self.empty():  # nothing was put between the check and registering
                    await asyncio.wait({future}, timeout=remaining)
            finally:
                self._waiters.discard(future)


class AsyncEvent(threading.Event):
    """A ``threading.Event`` a coroutine can also await."""

    def __init__(self) -> None:
        super().__init__()
        self._waiters = _Waiters()

    def set(self) -> None:
        super().set()
        self._waiters.wake()

    async def wait_async(self, timeout: float) -> bool:
        if self.is_set():
            return True
        future = self._waiters.add()
        try:
            if not self.is_set():
                await asyncio.wait({future}, timeout=timeout)
        finally:
            self._waiters.discard(future)
        return self.is_set()


class InputReady:
    """Wake the dispatcher after an empty or abandoned capture."""


class ConversationOutput:
    """A completed conversation response, not a synthetic user transcript."""

    def __init__(self, text: str, event_id: str, *, next_action: Optional[str] = None,
                 unheard: bool = False, transient: bool = False, no_barge_in: bool = False) -> None:
        self.text, self.event_id, self.next_action = text, event_id, next_action
        self.transient = transient  # not a message of the conversation (`say`): no transcript frames
        self.no_barge_in = no_barge_in  # the turn that wrote it called the tool
        self.unheard = unheard  # a reply barge-in cut before any of it played: spoken before the next request
        self.expires_at = float("inf")
        self.on_dequeue: Callable[[], None] = lambda: None
        self.after_playback: Optional[Callable[[], Awaitable[None]]] = None


class Aside(str):
    """Text a turn says that is not part of its reply (an approval question): spoken whole, and
    never sent as a transcript."""


class QuietTick:
    """Session-mode marker: the RECEIVED audio stream has been silent for another
    ``quiet_interval``; ``quiet_seconds`` is the received silence in the current listening phase:
    since the last speech, turn change, or resumed client stream."""

    __slots__ = ("quiet_seconds",)

    def __init__(self, quiet_seconds: float) -> None:
        self.quiet_seconds = quiet_seconds


class TailBarge:
    """Marker: speech tripped DURING the playback tail (after turn_done, before the client's
    state leaves `speaking`). The driver emits ``interrupted``; accepted user speech is captured separately."""

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
        self.turn_id: Optional[str] = None  # set when an interrupt already named the turn this becomes
        return self


def _default_transcribe(wav_path: str) -> str:
    return speech.transcribe_wav(wav_path)


def _session_state(method):
    """Serialize short state transitions; tool threads reach the session too."""
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._state_lock:
            return method(self, *args, **kwargs)
    return locked


class ConverseSession:
    """Drives the VAD → STT loop against a :class:`NetworkMicStream` as a task on the route's loop.

    ``transcribe(wav_path) -> str`` is the STT seam (default: the configured provider through
    :func:`speech.transcribe_wav`). The driver flips :meth:`set_playing` around TTS playback so the
    detector rejects speaker bleed; accepted speech interrupts the old reply and starts the next capture.
    """

    def __init__(
        self, np: Any, *, input_rate: int = DEFAULT_INPUT_RATE, quiet_interval: float = 0.0,
        transcribe: Optional[Callable[[str], str]] = None, barge_multiplier: Optional[float] = None,
        smart_turn_enabled: bool = False, endpoint_threshold: float = SMART_TURN_DEFAULT_THRESHOLD,
        endpoint_silence_ms: int = DEFAULT_ENDPOINT_SILENCE_MS,
        endpoint_max_silence_ms: int = DEFAULT_ENDPOINT_MAX_SILENCE_MS,
        vad_model: str = DEFAULT_VAD_MODEL,
        capture_no_data_seconds: float = DEFAULT_CAPTURE_NO_DATA_SECONDS,
    ) -> None:
        self._np = np
        self._endpointing = True
        self._endpointing_at_commit: Optional[bool] = None  # `on` sent with `commit`, applied when it is reached
        self._state_lock = threading.RLock()
        self.session_id = uuid.uuid4().hex  # echoed in `ready`, joinable across client/server traces
        self.device_name: Optional[str] = None  # trace only; set by the route
        # Trace bookkeeping: the wake claim the current exchange belongs to, the live turn trace,
        # the barge-in counters of the reply being played, and how its playback tail ended.
        self._claim_id: Optional[str] = None
        self._exchange_listening = False
        self._claim_granted = False
        self._claim_used = False
        self._turn_trace: Optional[tuple[str, Any]] = None
        self._barge: Optional[BargeStats] = None
        self._barge_done: Optional[BargeStats] = None
        self._tail_result: Optional[tuple[str, str, int]] = None  # turn id, drain.end, end ns
        self._transcribe = transcribe or _default_transcribe
        # Whisper resamples internally, so STT works at any capture rate; 30 ms blocks at that rate.
        self._input_rate = int(input_rate)
        self._capture_no_data_seconds = capture_no_data_seconds if capture_no_data_seconds > 0 else None
        # Session mode: quiet is measured in RECEIVED-audio silence, NOT wall clock. When no audio
        # arrives the task waits in stream.read_blocks() and this clock does not advance, so a client
        # holding the socket open (streaming only after a wake word) never accrues phantom quiet.
        self._quiet_interval = float(quiet_interval)
        self._quiet_seconds = 0.0
        self._quiet_held = False  # audio buffered during a capture is still being heard
        self._next_quiet_at = self._quiet_interval
        # Quiet is NOT accrued during a turn (the user has no window to speak into).
        self._turn_active = threading.Event()
        self._pending_turn = threading.Event()
        self._managed = False
        self._generation = 0
        self._active_turn_id: Optional[str] = None
        self._interrupt_callback: Optional[Callable[[], None]] = None
        self.on_tail_barge: Optional[Callable[[Optional[str]], None]] = None
        self._tail_ended = AsyncEvent()
        self._tail_ended.set()
        self._tail_interrupted = False
        # Playback tail: between turn_done and the client's state report (or a fallback) the client is
        # still PLAYING the reply; quiet is suppressed and onset must pass the echo guard.
        self._tail_active = False
        self._tail_turn_id: Optional[str] = None
        self._tail_deadline = 0.0
        # The agent's hold on the microphone for the turn under way (`mute`, `no_barge_in`): None,
        # "no_barge_in" (microphone audio does not interrupt) or "muted" (it is not heard at all).
        self._mic: Optional[str] = None
        self._wake_heard = False  # `heard` in a no_barge_in turn: what is said next is heard
        self._stop = threading.Event()
        self._playing = threading.Event()
        # Monotonic time the client's queued reply audio ends, from the sender's pacing.
        self.playback_until = 0.0
        self._tts_stop: Optional[threading.Event] = None
        self._interrupted = threading.Event()
        # Cancel: set by ``cancel()`` to abort an in-progress capture (its transcript is discarded,
        # no turn runs on it) and to stop an in-flight turn's TTS + agent without ending the session.
        self._cancelled = threading.Event()
        # True while the session is mid-capture, so ``cancel()`` knows whether aborting a capture
        # (and queuing a CancelTurn marker) applies to the capture happening right now.
        self._capturing = False
        self._continuation_capture = False
        # A granted wake: the next capture tolerates a pause between the wake word and the request.
        self._wake_pending = False
        self._queued_utterance: Optional[Utterance] = None
        self._active_utterance: Optional[Utterance] = None
        self.stream = NetworkMicStream(np, stop=self._stop)
        # The event stream the driver consumes: a transcript str, a QuietTick, a TailBarge, or the
        # None sentinel on shutdown.
        self.transcripts = AsyncQueue()
        self._block = int(self._input_rate * vad.BLOCK_MS / 1000)
        self._detector = SpeechDetector(np, model=vad_model, input_rate=self._input_rate, mult=barge_multiplier)
        self._level = InputLevel(np)  # specs/audio.md: one gain for this device, held through turns
        # The reply being played, as the server sent it (echo.py): how loud it is now, and its words.
        self._reply_level = ReplyLevel(np)
        self._reply_words: list[str] = []
        self._capture_over: Optional[list[str]] = None  # the reply the capture under way began over
        self._pre_roll: deque = deque(maxlen=max(1, vad.PRE_ROLL_MS // vad.BLOCK_MS))
        self._endpoint_blocks = max(1, math.ceil(endpoint_silence_ms / vad.BLOCK_MS))
        self._max_silence_blocks = max(self._endpoint_blocks, math.ceil(endpoint_max_silence_ms / vad.BLOCK_MS))
        self._wake_speech_blocks = math.ceil(WAKE_SPEECH_MS / vad.BLOCK_MS)
        self._max_blocks = max(1, vad.MAX_UTTERANCE_MS // vad.BLOCK_MS)
        self._pause_blocks = max(1, vad.PAUSE_MS // vad.BLOCK_MS)
        self._endpoint_threshold = float(endpoint_threshold)
        self._turn_detector: Any = None
        if smart_turn_enabled:
            try:
                from .turn_detector import load_turn_detector

                self._turn_detector = load_turn_detector(self._endpoint_threshold)
            except Exception:
                logger.warning("smart-turn endpointer failed to load; using the fixed endpoint", exc_info=True)
                self._turn_detector = None
        self._worker: Optional[asyncio.Task] = None
        self.on_trip: Optional[Callable[[str], None]] = None  # called with the phase name on every trip
        # While an approval is pending: takes captured speech (returns True when it is the answer),
        # and speech does not interrupt the waiting turn.
        self.answer_hook: Optional[Callable[[str], bool]] = None
        # Set when captured speech was taken as the answer; the next question clears it.
        self.answer_taken = False

    def awaiting_answer(self) -> bool:
        """The turn is waiting on the person: an approval's question has played out and no answer
        has been taken yet."""
        return (self.answer_hook is not None and not self.answer_taken
                and time.monotonic() >= self.playback_until + APPROVAL_LISTEN_GRACE_SECONDS)

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
            if self._barge is None or self._barge.turn_id != self._active_turn_id:
                self._close_barge()
                self._barge = BargeStats(self._active_turn_id)
            self._interrupted.clear()
            self._playing.set()
        else:
            self._playing.clear()
            if not self._turn_active.is_set():
                self._tts_stop = None

    def reply_audio(self, pcm: bytes, rate: int, start: float) -> None:
        """Driver hook: reply PCM16 sent, due to start playing at *start* (``time.monotonic()``)."""
        self._reply_level.sent(pcm, rate, start)

    def reply_text(self, text: str) -> None:
        """Driver hook: words about to be spoken in the reply being played."""
        self._reply_words.append(text)

    @_session_state
    def take_interrupted(self) -> bool:
        """Pop the barge-in flag; True when a trip cut playback since the last check."""
        if self._interrupted.is_set():
            self._interrupted.clear()
            return True
        return False

    # ── trace bookkeeping (never consulted by a decision) ──
    def _close_barge(self) -> None:
        if self._barge is not None:
            self._barge_done, self._barge = self._barge, None

    @_session_state
    def bind_trace(self, turn_id: Optional[str], turn_trace: Any) -> None:
        self._turn_trace = (turn_id, turn_trace) if turn_id and turn_trace is not None else None

    def _trace_of_active_turn(self) -> Any:
        current = self._tail_turn_id if self._tail_active else self._active_turn_id
        bound = self._turn_trace
        return bound[1] if bound is not None and current is not None and bound[0] == current else None

    @_session_state
    def note_claim(self, claim_id: str) -> None:
        """A wake claim arrived; the exchange it starts carries its id."""
        self._claim_id, self._claim_granted, self._claim_used = claim_id, False, False

    @_session_state
    def take_origin(self) -> tuple[Optional[str], str]:
        """``(claim_id, turn.source)`` for a turn that starts from speech: ``wake`` for the first
        turn after a granted claim, ``speech`` otherwise (a follow-up keeps the claim id)."""
        if self._claim_id is None:
            return None, "speech"
        source = "wake" if self._claim_granted and not self._claim_used else "speech"
        self._claim_used = self._claim_used or self._claim_granted
        return self._claim_id, source

    @_session_state
    def end_exchange(self) -> None:
        self._claim_id, self._claim_granted, self._claim_used = None, False, False
        self._exchange_listening = False

    @_session_state
    def output_next_action(self, requested: Optional[str]) -> str:
        """An unsolicited reply must not end an exchange already listening for follow-ups."""
        return requested or ("listen" if self._exchange_listening else "standby")

    @_session_state
    def record_next_action(self, action: str) -> None:
        self._exchange_listening = action == "listen"

    @_session_state
    def take_barge_summary(self, turn_id: str) -> dict[str, Any]:
        done = self._barge_done
        if self._barge is not None and self._barge.turn_id == turn_id and not self._tail_active:
            self._close_barge()
            done = self._barge_done
        if done is None or done.turn_id != turn_id:
            return {}
        self._barge_done = None
        return done.summary()

    @_session_state
    def take_tail_result(self, turn_id: str) -> Optional[tuple[str, int]]:
        """``(drain.end, end ns)`` of the playback tail that followed *turn_id*, once it has ended."""
        if self._tail_active and self._tail_turn_id == turn_id and self._stop.is_set():
            self.end_playback_tail("disconnected")
        result = self._tail_result
        if result is None or result[0] != turn_id:
            return None
        self._tail_result = None
        return result[1], result[2]

    def stop(self) -> None:
        """End the loop and unblock the reader and any transcript waiter."""
        self._stop.set()
        self.stream.close()
        self._tail_ended.set()
        self.transcripts.put(None)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def commit(self) -> None:
        """End the current capture now (client sent ``commit``), bypassing Smart Turn and the wake
        pause. The marker is one silent block queued after the audio already received, so it also
        wakes a capture that is waiting for audio."""
        self.stream.feed(self._np.zeros(self._block, dtype=self._np.int16).tobytes(), commit=True)

    def set_endpointing(self, on: Optional[bool], *, commit: bool = False) -> None:
        """Whether pauses end the utterance. Off, only ``commit`` (or the utterance cap) does.
        With *commit*, ``on`` applies where the commit marker is reached, so audio sent before it
        is still heard with endpointing as it was."""
        if commit:
            self._endpointing_at_commit = on
            self.commit()
        elif on is not None:
            self._endpointing = on

    def _commit_reached(self) -> None:
        self.stream.committed = False
        if self._endpointing_at_commit is not None:
            self._endpointing, self._endpointing_at_commit = self._endpointing_at_commit, None

    def attach_device(self, device_name: Optional[str]) -> None:
        """The device this session's microphone belongs to: its input gain starts from what was learned before."""
        self._level.attach(device_name)

    @_session_state
    def ignore_microphone(self, *, muted: bool) -> None:
        """Agent tool hook, for the turn under way until its playback ends: microphone audio does
        not interrupt it, and with *muted* is not heard at all."""
        if muted or self._mic != "muted":
            self._mic = "muted" if muted else "no_barge_in"

    @_session_state
    def unmute(self) -> None:
        """Agent tool hook: a later `standby` in the turn replaced its `mute`."""
        if self._mic == "muted":
            self._mic = None

    @_session_state
    def heard(self) -> None:
        """The device heard a wake word. In a `no_barge_in` turn that is the person interrupting:
        the reply stops and what they say next is captured."""
        if self._mic == "no_barge_in" and (self._turn_active.is_set() or self._tail_active):
            self._mic, self._wake_heard = None, True

    @_session_state
    def note_wake(self, claim_id: Optional[str] = None) -> None:
        """A wake was granted: the next capture waits through a pause after the wake word."""
        self._mic = None  # a new activation is the person's, whatever the last turn asked for
        self._wake_pending = True
        if claim_id is not None and claim_id == self._claim_id:
            self._claim_granted = True

    # ── input task ──
    def start(self) -> None:
        """Run the session's input as a task on the running loop. The task keeps the request's
        contextvars, so STT reads provider credentials through the profile scope."""
        self._worker = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        interrupt = None
        try:
            while not self._stop.is_set():
                interrupt = None
                if self._tail_active and time.monotonic() >= self._tail_deadline:
                    self.end_playback_tail("timeout")  # client never reported its state: don't stay suppressed forever
                onset = await self._listen()
                if onset is None:
                    continue
                recorder, wake, capture_generation, interrupt = onset
                try:
                    transcript, _volume = await self._capture_and_transcribe(recorder, wake=wake)
                except BaseException:
                    with self._state_lock:
                        self._capturing = self._continuation_capture = False
                        self.release_capture()
                    if interrupt is not None:
                        interrupt.resolve(None)
                    raise
                # Audio received during the capture and its transcription is not idle silence.
                self._quiet_held = True
                self.stream.mark_resumed()
                with self._state_lock:
                    abandoned = self._capture_abandoned(capture_generation)
                    self._capturing = self._continuation_capture = False
                    if abandoned:
                        self._cancelled.clear()
                        recorder.set(outcome="cancelled")
                        emit_turn_trace(recorder)
                        self.release_capture()
                        if interrupt is not None:
                            interrupt.resolve(None)
                    elif transcript and self._capture_over and says_reply(transcript, " ".join(self._capture_over)):
                        # The reply's own words, heard while it played: the device's echo, not a request.
                        logger.info("converse: capture over a reply was its echo; dropped (session=%s, %d chars)",
                                    self.session_id, len(transcript))
                        recorder.set(outcome="echo")
                        emit_turn_trace(recorder)
                        self.release_capture()
                        if interrupt is not None:
                            interrupt.resolve("echo")
                    elif transcript and self._answers_approval(transcript):
                        recorder.set(outcome="answer")
                        emit_turn_trace(recorder)
                        self.release_capture()
                        if interrupt is not None:
                            interrupt.resolve("empty")
                    elif transcript:
                        if self._managed:
                            self._pending_turn.set()
                        utterance = Utterance(transcript, recorder)
                        if interrupt is not None:
                            utterance.turn_id = interrupt.next_turn_id
                            interrupt.resolve("request")
                        self._queued_utterance = utterance
                        self.transcripts.put(utterance)
                    else:
                        recorder.set(outcome="empty")
                        emit_turn_trace(recorder)
                        self.release_capture()
                        if interrupt is not None:
                            interrupt.resolve("empty")
        except Exception:
            logger.warning("converse VAD loop failed", exc_info=True)
        finally:
            if interrupt is not None:
                interrupt.resolve(None)
            self.transcripts.put(None)

    async def _listen(self) -> Optional[tuple[PhaseRecorder, bool, int, Any]]:
        """Run onset detection over the blocks received so far. When speech starts, the blocks
        after it go back to the stream for the capture, and this returns the capture's
        ``(recorder, wake, generation, interrupt)``."""
        blocks = await self.stream.read_blocks(self._block)
        commit_from = self.stream.commit_from
        if self._stop.is_set():
            return None
        with self._state_lock:
            epoch = self.stream._epoch
            if self.stream._read_epoch != epoch:
                return None  # samples received before the last drain/reset
            if self.stream.resumed:
                # A new listening phase: silence before the client stopped sending is not
                # part of this phase's quiet_interval.
                self.stream.resumed = False
                self._reset_quiet()
            busy = self._turn_active.is_set() or self._pending_turn.is_set() or self._tail_active
            learn = not busy and not self.playing()
            # A pending approval whose question has played out listens for its answer as
            # ordinary listening: the open turn is not playback, and a short "yes" counts.
            answering = (self.answer_hook is not None
                         and time.monotonic() >= self.playback_until + APPROVAL_LISTEN_GRACE_SECONDS)
            playing = (self.playing() or self._tail_active) and not answering
            if not busy:
                self._mic = None  # the turn it was for is over
            held, woken, self._wake_heard = self._mic, self._wake_heard and busy, False
            reference = self._reply_level.at(time.monotonic()) if playing else None
        heard = await on_audio_thread(self._detect, blocks, learn, playing, busy and not answering, reference)
        with self._state_lock:
            if self._stop.is_set() or self.stream._epoch != epoch:
                return None  # stopped, or drained while the blocks were being heard
            if len(heard) < len(blocks):
                self.stream.unread(self._np.concatenate(blocks[len(heard):]))
            for i, (data, phase, gain, lv, probability, th) in enumerate(heard):
                if held is not None and held == self._mic and not answering:
                    phase = None  # the agent holds the microphone for this turn
                elif woken and i == 0 and phase is None:
                    phase = "playback" if playing else "generation"
                if self._barge is not None:
                    # traces carry levels as received (level.py)
                    self._barge.add(self._block * 1000 / self._input_rate, None if lv is None else lv / gain,
                                    probability, None if th is None else th / gain)
                interrupt = None
                if phase is not None:
                    reply_phase = ("answer" if answering
                                   else "drain" if self._tail_active else "speaking" if playing
                                   else "thinking" if self._turn_active.is_set()
                                   else "pending" if busy else "idle")
                    logger.info("converse onset: session=%s turn=%s detector=%s reply_phase=%s probability=%s",
                                self.session_id, self._active_turn_id, self._detector.name, reply_phase,
                                probability)
                    if reply_phase in ("thinking", "speaking", "drain"):
                        interrupt = begin_speech_interrupt(self._trace_of_active_turn(), reply_phase,
                                                           self._detector, gain)
                # Onset filtering rejects echo/noise; accepted speech always owns a capture.
                if self._mic != "muted":
                    self._pre_roll.append(data.copy())
                if phase is None:
                    if commit_from is None or i >= commit_from:
                        self._commit_reached()  # a commit with no utterance to end
                    self._account_received_silence()
                    continue
                self._reset_quiet()
                if self.on_trip is not None:
                    try:
                        self.on_trip(phase)
                    except Exception:
                        logger.debug("converse on_trip callback failed", exc_info=True)
                self._continuation_capture = busy
                self._capture_over = self._reply_words if playing else None
                wake, self._wake_pending = self._wake_pending, False
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
                    self.end_playback_tail("interrupted")
                elif self._turn_active.is_set() and self.answer_hook is None:
                    self._trigger_barge_in()
                recorder = PhaseRecorder(input_rate=self._input_rate, session_mode=self._quiet_interval > 0,
                                         **{"session.id": self.session_id, "input.continuation": busy,
                                            "input.gain_db": round(self._level.gain_db, 1)})
                return recorder, wake, self._generation, interrupt
        return None

    def _detect(self, blocks: list, learn: bool, playing: bool, interrupting: bool,
                reference: Optional[float] = None) -> list[tuple]:
        """Audio thread: each block at the session's gain through the speech detector, up to the
        first block where speech starts. One ``(data, phase, gain, level, probability, threshold)``
        per block heard."""
        detector, heard = self._detector, []
        for raw in blocks:
            data = self._level.apply(raw, learn=learn)
            phase = detector.feed(data, playing, interrupting=interrupting, reference=reference)
            heard.append((data, phase, self._level.gain, getattr(detector, "level", None),
                          getattr(detector, "probability", None), getattr(detector, "threshold", None)))
            if phase is not None:
                break
        return heard

    def _answers_approval(self, transcript: str) -> bool:
        hook = self.answer_hook
        if hook is None:
            return False
        try:
            taken = bool(hook(transcript))
            if taken:
                self.answer_taken = True
            return taken
        except Exception:
            logger.debug("converse approval answer hook failed", exc_info=True)
            return False

    @_session_state
    def _capture_abandoned(self, generation: int) -> bool:
        return (self._stop.is_set() or self._cancelled.is_set()
                or (not self._continuation_capture and (generation != self._generation
                    or self._turn_active.is_set() or self._tail_active)))

    def _account_received_silence(self) -> None:
        """One block of received non-speech audio elapsed. In session mode, accrue it and emit a
        :class:`QuietTick` each time the received silence crosses another quiet_interval."""
        if (self._quiet_interval <= 0 or self._quiet_held or self._turn_active.is_set()
                or self._pending_turn.is_set() or self._tail_active):
            return
        self._quiet_seconds += self._block / self._input_rate  # == 0.03 s per block
        if self._quiet_seconds + 1e-6 >= self._next_quiet_at:
            self._claim_id = None  # the exchange a wake started has gone quiet
            self.transcripts.put(QuietTick(round(self._quiet_seconds, 1)))
            self._next_quiet_at += self._quiet_interval

    @_session_state
    def set_quiet_interval(self, interval: float) -> None:
        """Apply the replacement socket's requested quiet cadence without changing its reply."""
        self._quiet_interval = float(interval)
        self._reset_quiet()

    def _reset_quiet(self) -> None:
        self._quiet_held = False
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
            self._trigger_barge_in(stop_run=self._cancelled.is_set())

    @_session_state
    def begin_turn(self, turn_id: Optional[str] = None, *, tts_stop=None, on_interrupt=None) -> None:
        """Bind cancellation from thinking through playback to this exact turn."""
        if self._active_turn_id != turn_id or not self._turn_active.is_set():
            self._generation += 1
            self._reply_words = []  # a capture begun over the last reply keeps that reply's words
        self._active_turn_id = turn_id
        self._tts_stop = tts_stop
        self._interrupt_callback = on_interrupt
        self._turn_active.set()
        self._pending_turn.clear()
        self._reset_quiet()

    @_session_state
    def end_turn(self) -> None:
        if not self._tail_active:
            self._close_barge()
            self._active_turn_id = None
            self._mic = None
        self._turn_active.clear()
        self._active_utterance = None
        if not self._tail_active:
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
        self._tail_ended.clear()
        self._tail_interrupted = False
        self._tail_active = True
        self._tail_turn_id = turn_id
        self._active_turn_id = turn_id
        self._tail_deadline = time.monotonic() + fallback_seconds

    @_session_state
    def end_playback_tail(self, reason: str = "reported") -> None:
        if self._tail_active and self._tail_turn_id is not None:
            self._tail_result = (self._tail_turn_id,
                                 "interrupted" if self._tail_interrupted else reason, time.time_ns())
        self._close_barge()
        self._tail_active = False
        self._tail_turn_id = None
        self._mic = None
        if not self.capture_pending():
            self.stream.discard()
            self._pre_roll.clear()
        self._tail_ended.set()
        self._active_turn_id = None
        self._tts_stop = None
        self._reset_quiet()

    @_session_state
    def playback_ended(self, turn_id: Optional[str] = None) -> None:
        """Client hook (its ``state`` left ``speaking``): ends the playback tail for the matching
        turn (or the current tail when no id is given)."""
        if self._tail_active and (turn_id is None or turn_id == self._tail_turn_id):
            self.end_playback_tail()

    async def wait_for_playback_tail(self) -> None:
        if self._tail_active:
            remaining = max(0, self._tail_deadline - time.monotonic())
            await self._tail_ended.wait_async(remaining)
            if self._tail_active:
                self.end_playback_tail("disconnected" if self._stop.is_set() else "timeout")

    @_session_state
    def _interrupt_tail(self) -> None:
        if self._tail_interrupted:
            return
        self._tail_interrupted = True
        if self._tts_stop is not None:
            self._tts_stop.set()  # invalidate deferred actions while the final reply still plays
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
            self._trace_cancel("drain")
            self._interrupt_tail()
            return True
        if self._turn_active.is_set() or self._tts_stop is not None:
            self._trace_cancel("speaking" if self.playing() else "thinking")
            self._trigger_barge_in(stop_run=True)
            return True
        self._cancelled.set()
        if self._capturing:
            self.commit()
            self.transcripts.put(CancelTurn(turn_id))
        return True

    def _trace_cancel(self, phase: str) -> None:
        turn_trace = self._trace_of_active_turn()
        if turn_trace is not None:
            turn_trace.event("interrupt", **{"interrupt.cause": "cancel", "interrupt.phase": phase})

    @_session_state
    def _trigger_barge_in(self, *, stop_run: bool = False) -> None:
        """Cut the in-flight reply exactly once, including before its first audio. *stop_run*
        (``cancel``) also stops the agent run; speech leaves it running for the words to reach."""
        if self._interrupted.is_set():
            return
        self._mic = None  # an interrupted turn mutes nothing and protects nothing
        if self._interrupt_callback is not None:  # before tts_stop: the driver reads the cause with it
            self._interrupt_callback(stop_run)
        if self._tts_stop is not None:
            self._tts_stop.set()
        self._playing.clear()
        self._interrupted.set()

    async def _capture_and_transcribe(self, recorder: Optional[PhaseRecorder] = None, *,
                                      wake: bool = False) -> tuple[str, float]:
        """Endpoint the utterance from the pre-roll and return ``(transcript, volume)`` — ``volume``
        is the captured audio's RMS loudness (:func:`vad.utterance_volume`). *wake* marks the first
        capture after a granted wake. Wake selection never gates this capture."""
        generation = self._generation
        recorder = recorder or PhaseRecorder()
        with recorder.phase("voice.capture", **{
                "endpoint.mode": ("commit" if not self._endpointing
                                  else "smart_turn" if self._turn_detector is not None else "fixed"),
                "endpoint.silence_ms": self._blocks_ms(self._endpoint_blocks),
                "endpoint.max_silence_ms": self._blocks_ms(self._max_silence_blocks),
                "endpoint.wake": wake,
        }) as cap:
            wav_path, volume = await self._capture(wake=wake, phase=cap)
            cap.set(**{"vad.model": self._detector.name})
        self._pre_roll.clear()  # capture drained the pre-roll into the WAV
        if self.stream.no_data_return or self._capture_abandoned(generation):
            vad.unlink_quietly(wav_path)
            return "", volume
        with recorder.phase("voice.stt") as stt:
            try:
                transcript = str(await asyncio.to_thread(self._transcribe, wav_path) or "").strip()
            except Exception:
                logger.warning("converse transcription raised", exc_info=True)
                transcript = ""
            finally:
                vad.unlink_quietly(wav_path)
            stt.set(**{"stt.transcript_chars": len(transcript), "stt.success": bool(transcript)})
        return transcript, volume

    def _blocks_ms(self, blocks: int) -> float:
        return blocks * self._block * 1000 / self._input_rate

    async def _capture(self, *, wake: bool = False, phase: Any = None) -> tuple[str, float]:
        """Read from the pre-roll until the utterance ends; returns ``(wav_path, volume)``.

        A pause of ``endpoint_silence_ms`` is a candidate endpoint. Without Smart Turn it ends the
        capture. With Smart Turn, a complete-sounding turn ends it; an incomplete one keeps listening
        and is checked again after each further window. ``endpoint_max_silence_ms`` of continuous
        silence ends it regardless. After a wake, a pause before ``WAKE_SPEECH_MS`` of speech waits
        for that maximum, so the user may pause after the wake word. Client ``commit`` ends it at once.
        A capture that reaches ``MAX_UTTERANCE_MS`` ends at its last pause of ``PAUSE_MS``, and what
        followed that pause goes back to the stream to begin the next capture.
        In push-to-talk mode neither silence nor Smart Turn ends it: only ``commit`` does.

        The blocks are heard on the audio thread, as many as have arrived at a time; those after
        the block that decides go back to the stream.
        """
        np = self._np
        c = _Capture(list(self._pre_roll), self._endpoint_blocks)
        reason = "max_utterance"
        while c.blocks() < self._max_blocks:
            if self.stream.committed:
                # Reached the commit marker (maybe while draining audio after Smart Turn): keep what
                # has arrived and end without waiting for more audio or asking Smart Turn.
                c.frames.extend(await on_audio_thread(self._gained, self.stream.ready_blocks(self._block)))
                reason = "client_commit"
                break
            blocks = await self.stream.read_blocks(self._block, no_data_seconds=self._capture_no_data_seconds)
            if self.stream.no_data_return or self._stop.is_set():
                reason = "no_data" if self.stream.no_data_return else "stopped"
                break
            commit_from = self.stream.commit_from
            heard = blocks if commit_from is None else blocks[:commit_from]
            taken, decision = await on_audio_thread(self._take, c, heard, wake)
            if decision is not None:
                if taken < len(blocks):
                    self.stream.unread(np.concatenate(blocks[taken:]))
            elif commit_from is not None:
                c.frames.extend(await on_audio_thread(self._gained, blocks[commit_from:]))
                continue
            if decision == "max_silence":
                reason = "max_silence"
                break
            if decision != "check":
                continue
            c.next_check += self._endpoint_blocks
            if self._turn_detector is None:
                reason = "silence"
                break
            try:
                prob = await on_audio_thread(self._turn_probability, c.frames)
            except Exception:
                logger.debug("smart-turn inference failed; committing on silence", exc_info=True)
                reason = "inference_error"
                break
            complete = prob >= self._endpoint_threshold
            pause_ms = self._blocks_ms(c.quiet)
            if complete:
                # Speech that arrived while the model ran belongs to this turn: keep listening.
                spoken = c.speech_blocks
                await on_audio_thread(self._take, c, self.stream.ready_blocks(self._block), wake, False)
                complete = c.speech_blocks == spoken
            if phase is not None:
                phase.event("endpoint.decision", **{
                    "endpoint.probability": prob, "endpoint.threshold": self._endpoint_threshold,
                    "endpoint.decision": "commit" if complete else "hold", "endpoint.pause_ms": pause_ms,
                    "endpoint.reason": "threshold" if complete else "resumed" if prob >= self._endpoint_threshold
                    else "incomplete"})
            vad.vad_log(f"smart-turn: p={prob:.3f} after {pause_ms:.0f} ms -> "
                        f"{'commit' if complete else 'hold'}")
            if complete:
                reason = "threshold"
                break
        if reason == "max_utterance" and c.split and not self.stream.committed:
            # End at the last pause instead of mid-word; the rest is heard again as the next capture.
            self.stream.unread(np.concatenate(c.received[c.split - c.pre_roll:]))
            del c.frames[c.split:]
        if self.stream.committed:
            self._commit_reached()  # a commit ends only the capture it arrived in
        if phase is not None:
            phase.set(**{"endpoint.reason": reason, "endpoint.pause_ms": self._blocks_ms(c.quiet),
                         "endpoint.longest_pause_ms": self._blocks_ms(c.longest_pause)})
        logger.info("converse endpoint: session=%s mode=%s wake=%s reason=%s pause_ms=%.0f longest_pause_ms=%.0f "
                    "speech_ms=%.0f", self.session_id, "smart_turn" if self._turn_detector is not None else "fixed",
                    wake, reason, self._blocks_ms(c.quiet), self._blocks_ms(c.longest_pause),
                    self._blocks_ms(c.speech_blocks))
        wav_path, volume, samples = await on_audio_thread(self._write_capture, c.frames)
        if phase is not None:
            phase.set(**{"input.audio_ms": samples * 1000 / self._input_rate})
        return wav_path, volume

    def _gained(self, blocks: list) -> list:
        """Audio thread: *blocks* at the session's gain."""
        return [self._level.apply(raw, learn=False).copy() for raw in blocks]

    def _take(self, c: "_Capture", blocks: list, wake: bool, decide: bool = True) -> tuple[int, Optional[str]]:
        """Audio thread: add *blocks* to the capture. With *decide*, stop after the block that
        reaches the utterance cap, ``max_silence`` or a candidate endpoint (``check``); returns how
        many blocks were taken and which of those stopped it."""
        for i, raw in enumerate(blocks):
            if decide and c.blocks() >= self._max_blocks:
                return i, "max_utterance"
            data = self._level.apply(raw, learn=False)
            c.received.append(raw)
            c.frames.append(data.copy())
            if self._detector.is_quiet(data):
                c.quiet += 1
            else:
                c.longest_pause = max(c.longest_pause, c.quiet)  # a pause the user spoke through
                if c.speech_blocks and c.quiet >= self._pause_blocks:
                    c.split = len(c.frames) - 1 - c.quiet // 2  # the middle of that pause
                c.quiet, c.next_check = 0, self._endpoint_blocks
                c.speech_blocks += 1
            if not decide or not self._endpointing:
                # With endpointing off the device, not the audio, ends the utterance: `commit` is
                # the only natural endpoint. The utterance cap still applies above.
                continue
            if c.quiet >= self._max_silence_blocks:
                return i + 1, "max_silence"
            if c.quiet >= c.next_check and not (wake and c.speech_blocks < self._wake_speech_blocks):
                return i + 1, "check"
        return len(blocks), None

    def _turn_probability(self, frames: list) -> float:
        """Audio thread: Smart Turn's probability that the turn in *frames* is complete."""
        return self._turn_detector.turn_complete_probability(self._frames_to_f32_16k(frames))

    def _write_capture(self, frames: list) -> tuple[str, float, int]:
        """Audio thread: the capture as a WAV, its volume and its length in samples."""
        np = self._np
        audio = np.concatenate(frames, axis=0)
        return (vad.write_wav(np, audio, sample_rate=self._input_rate), vad.utterance_volume(np, audio),
                len(audio))

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


class _Capture:
    """One capture's audio and endpoint counters, shared by the session's task and the audio
    thread in turn."""

    def __init__(self, pre_roll: list, endpoint_blocks: int) -> None:
        self.frames = pre_roll
        self.pre_roll = len(pre_roll)
        self.received: list = []  # the blocks after the pre-roll as they arrived, before the session's gain
        self.quiet = self.speech_blocks = self.longest_pause = self.split = 0
        self.next_check = endpoint_blocks

    def blocks(self) -> int:
        return len(self.frames) - self.pre_roll


# ── start frame ──

def clamp_sample_rate(raw: Any, default: int) -> int:
    """An integer sample rate held to [8000, 48000]; *default* for anything that is not an integer."""
    if type(raw) is not int:
        return default
    return max(SAMPLE_RATE_MIN, min(raw, SAMPLE_RATE_MAX))


def parse_quiet_interval(raw: Any) -> float:
    """The SESSION-mode opt-in from the ``start`` frame: seconds of received quiet between turns
    before a ``{"type":"quiet"}`` advisory. Absent /
    invalid / ``<= 0`` → ``0.0`` = continuous mode, the always-listening behaviour."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        return 0.0
    return 0.0 if raw <= 0 else min(float(raw), QUIET_INTERVAL_MAX)


def parse_start_config(frame: dict[str, Any]) -> tuple[int, int, float]:
    """``(input_rate, output_rate, quiet_interval)`` from a ``start`` frame. All three are
    optional; rates are clamped to [8000, 48000]. The names are parsed separately and required,
    see :func:`~.named.parse_names`."""
    input_rate = clamp_sample_rate(frame.get("input_rate"), DEFAULT_INPUT_RATE)
    output_rate = clamp_sample_rate(frame.get("output_rate"), DEFAULT_OUTPUT_RATE)
    quiet_interval = parse_quiet_interval(frame.get("quiet_interval"))
    return input_rate, output_rate, quiet_interval


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
    release_turn: Optional[Callable[[bool], None]]
    turn_timeout: float
    tail_fallback: float
    max_tts_chars: int
    standby: Optional[Callable[[], bool]]
    mute: Optional[Callable[[], bool]]
    transcripts: bool
    tool_calls: Optional[Callable[[], int]]
    reasoning_tokens: Optional[Callable[[], Optional[int]]]
    last_progress: Optional[Callable[[], float]]
    message_ids: Any = None  # reads Hermes's ids for the turn's messages (catchup.MessageIds)
    present: Optional[Callable[[], bool]] = None  # a device is attached to a stopped session's turn
    turn: Any = None  # the turn in flight

    def gone(self) -> bool:
        """Nobody is listening: no connection is attached to the turn. A detached turn keeps its
        session for the replacement connection, so a running session is not proof of a listener."""
        if self.present is not None:
            return not self.present()
        return self.session.stopped


class _Piece:
    """Marks where one piece of reply text starts in ``pcm_q``; its audio follows it."""
    __slots__ = ("epoch", "frame")

    def __init__(self, epoch: int, frame: Optional[dict]):
        self.epoch, self.frame = epoch, frame


class _Turn:
    """One agent turn's pipeline: the agent task feeds ``text_q``, the TTS task turns it
    into PCM on ``pcm_q``, and the driver streams ``pcm_q`` out. ``ns`` holds the phase marks."""

    def __init__(self, transcript: str, turn_id: str, meta: dict[str, Any]) -> None:
        self.transcript, self.turn_id, self.meta = transcript, turn_id, meta
        self.input_text = str(transcript)
        self.agent_started = False
        self.text_q = AsyncQueue()  # deltas; None = turn done
        self.pcm_q: asyncio.Queue[Optional[bytes]] = asyncio.Queue()  # PCM out; None = done
        self.tts_stop = threading.Event()
        self.reply_parts: list[str] = []
        self.spoken_parts: list[str] = []
        self.result: dict[str, str] = {}
        self.ns: dict[str, int] = {"turn_start": time.time_ns()}
        self.output: Optional[ConversationOutput] = None
        self.transient = False  # the output is not a message of the conversation
        self.pcm_chunks = 0
        self.timed_out = False
        self.progress_at = 0.0  # monotonic time of the last reply text
        self.user_frame: Optional[asyncio.Future] = None  # sends the user transcript once it has an id
        self.user_id: Optional[str] = None
        self.assistant_id: Optional[str] = None
        self.state: Optional[str] = None  # the last `state` frame sent: thinking, speaking, listening
        self.released = False  # speech barged in: the run goes on for the person's words to reach
        self.unheard: Optional[str] = None  # a finished reply cut before any of it played
        self.settled = asyncio.Event()  # the agent run has ended: Hermes has stored what it will
        # A reconnect replays the reply so far as text and starts a new epoch: text received
        # before it is never spoken after it (client contract, "Reconnecting during a turn").
        self.speech_epoch = 0
        self.replayed_deltas = 0  # deltas received before the last reconnect
        self.skipping = False  # the driver is dropping audio of a piece from an earlier epoch
        self.shown: list[str] = []  # the reply's text so far: pieces sent, and text nobody was there for
        self.respeak = False  # a new connection took the turn: its next audio follows a new `speaking`

    def on_delta(self, delta: str) -> None:
        # Called from run_turn's execution context (loop coroutine or a worker thread); thread-safe.
        if delta:
            self.progress_at = time.monotonic()
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
    release_turn: Optional[Callable[[bool], None]] = None,
    turn_timeout: float = DEFAULT_TURN_TIMEOUT,
    tail_fallback: float = DEFAULT_TAIL_FALLBACK,
    max_tts_chars: int = DEFAULT_MAX_TTS_CHARS,
    standby: Optional[Callable[[], bool]] = None,
    mute: Optional[Callable[[], bool]] = None,
    transcripts: bool = False,
    tool_calls: Optional[Callable[[], int]] = None,
    reasoning_tokens: Optional[Callable[[], Optional[int]]] = None,
    last_progress: Optional[Callable[[], float]] = None,
    message_ids: Any = None,
    present: Optional[Callable[[], bool]] = None,
    after_turn: Optional[Callable[[], Awaitable[None]]] = None,
    control: Optional[dict] = None,
) -> None:
    """Run the per-transcript incremental-TTS turn loop.

    One turn per transcript pulled off ``session.transcripts``: announce it (``transcript``), run
    the agent turn via ``run_turn(transcript, on_delta, *, interrupted) -> (reply_text, err)``
    (deltas stream out as they land), and speak the reply INCREMENTALLY — each sentence is
    synthesized and sent the moment it is ready. Control-frame order per turn:
    ``transcript`` → ``thinking`` → (``speaking`` + PCM frames) → optional ``interrupted``/``error``
    → ``turn_done``.

    ``thinking`` repeats every five seconds with the observed tool-start count and reported
    reasoning tokens until terminal frames. ``tool_calls`` and ``reasoning_tokens`` read
    the current run; prepared replies report zero tool calls and ``None`` reasoning tokens.

    BARGE-IN: accepted speech over a reply stops its TTS playback (``session`` sets ``tts_stop``)
    and we emit ``interrupted``. When the host provides ``release_turn(heard)``, the agent run keeps
    going so the person's words reach it as the next request, as a Telegram message typed mid-run
    does; a reply that finished with none of it played is spoken before that request. ``cancel``
    stops the run through ``interrupt_turn``.

    """
    d = _Driver(session=session, synth=synth, cap=cap, loop=loop, send_json=send_json,
                send_bytes=send_bytes, run_turn=run_turn, history=history,
                quiet_interval=quiet_interval, interrupt_turn=interrupt_turn, release_turn=release_turn,
                turn_timeout=turn_timeout, tail_fallback=tail_fallback, max_tts_chars=max_tts_chars,
                standby=standby, mute=mute, transcripts=transcripts, tool_calls=tool_calls,
                reasoning_tokens=reasoning_tokens, last_progress=last_progress,
                message_ids=message_ids, present=present)
    session.enable_turn_control()

    async def resume(transcripts: bool) -> None:
        """A new connection took over: tell it where the turn in flight stands."""
        d.transcripts = transcripts
        turn = d.turn
        if turn is None:
            return
        text = "" if turn.transient else speech.strip_markdown("".join(turn.reply_parts)).strip()
        # Text so far is in the snapshot: none of it is spoken after this (contract, section 4).
        turn.shown = [text] if text else []
        turn.replayed_deltas, turn.speech_epoch, turn.skipping = len(turn.reply_parts), turn.speech_epoch + 1, True
        if transcripts and text:
            await send_json({"type": "transcript", "role": "assistant", "turn_id": turn.turn_id,
                             "text": text, "replay": True})
        state, turn.state, turn.respeak = "listening" if turn.state == "listening" else "thinking", None, True
        await _send_state(d, turn, state)
        await send_json(_thinking_frame(d, turn))
    if control is not None:
        control["resume"] = resume
        control["active"] = lambda: d.turn is not None or session._tail_active

    async def notify_tail_barge(turn_id):
        await send_json({"type": "interrupted", "turn_id": turn_id})
        await send_json({"type": "turn_done", "turn_id": turn_id})

    def tail_barge(turn_id):
        async def notify():
            try:
                await notify_tail_barge(turn_id)
            except Exception:
                logger.debug("tail interrupt delivery failed", exc_info=True)
        def start():
            task = asyncio.create_task(notify())
            tail_notices.add(task)
            task.add_done_callback(tail_notices.discard)
        loop.call_soon_threadsafe(start)
    session.on_tail_barge = tail_barge
    tail_notices: set[asyncio.Task] = set()

    async def old_turn_closed() -> None:
        """A reply cut while it played gets its `interrupted` and `turn_done` before the next
        turn's first frame."""
        if tail_notices:
            await asyncio.gather(*tail_notices)
    deferred_outputs = deque()
    unheard: list[ConversationOutput] = []  # spoken before the next request (or once capture settles)
    unsubmitted = []

    async def speak_output(item: ConversationOutput) -> None:
        item.on_dequeue()
        if item.expires_at <= time.time():
            return
        turn_id = uuid.uuid4().hex
        tt = start_turn(recorder=None, turn_id=turn_id,
                        **{"session.id": getattr(session, "session_id", None)})
        turn = _Turn("", turn_id, {"turn_id": turn_id, "event_id": item.event_id})
        turn.output, turn.transient = item, item.transient
        if tt.traceparent:
            turn.meta["traceparent"] = tt.traceparent
        _open_trace(session, tt, turn_id, "server")
        try:
            await _run_turn(d, turn, tt)
            await session.wait_for_playback_tail()
            if item.after_playback is not None and not session.stopped and not turn.tts_stop.is_set():
                await item.after_playback()
        except asyncio.CancelledError:
            tt.set_missing(outcome="interrupted", interrupted=True)
            raise
        except Exception:
            tt.set_missing(outcome="error")
            raise
        finally:
            _close_trace(session, tt, turn)

    async def speak_unheard() -> None:
        while unheard:
            await speak_output(unheard.pop(0))

    while not session.stopped and not (control or {}).get("detached", False):
        if deferred_outputs and not session.capture_pending():
            item = deferred_outputs.popleft()
        else:
            item = await session.transcripts.get_async()
        if item is None:  # shutdown sentinel
            break
        if isinstance(item, InputReady):
            if unheard and not session.capture_pending():
                await speak_unheard()  # the words that barged in were nothing to answer
            continue
        if isinstance(item, QuietTick):
            await send_json({"type": "quiet", "quiet_seconds": item.quiet_seconds})
            continue
        if isinstance(item, TailBarge):
            await notify_tail_barge(item.turn_id)
            continue
        if isinstance(item, CancelTurn):
            unsubmitted.clear()
            unheard.clear()
            # A capture abandoned with no turn running: its audio is dropped and no frame says so.
            continue
        if isinstance(item, ConversationOutput):
            if session.capture_pending():
                (unheard if item.unheard else deferred_outputs).append(item)
                continue
            await old_turn_closed()
            await speak_output(item)
            continue
        transcript = item
        if not transcript:
            continue
        await old_turn_closed()
        await speak_unheard()
        turn_id = getattr(item, "turn_id", None) or uuid.uuid4().hex
        tt = start_turn(recorder=getattr(item, "recorder", None), turn_id=turn_id,
                        **{"session.id": getattr(session, "session_id", None)})
        take_origin = getattr(session, "take_origin", None)
        claim_id, source = take_origin() if take_origin is not None else (None, "speech")
        _open_trace(session, tt, turn_id, source, claim_id)
        meta: dict[str, Any] = {"turn_id": turn_id}
        if tt.traceparent:
            meta["traceparent"] = tt.traceparent
        turn = _Turn(transcript, turn_id, meta)
        turn.input_text = "\n".join([*unsubmitted, str(transcript)])
        try:
            session.reserve_turn(turn_id, turn.tts_stop, transcript)
            if d.transcripts and d.message_ids is not None:
                # Hermes writes the user message as the agent starts; the frame follows its id.
                turn.user_frame = asyncio.ensure_future(_send_user_transcript(d, turn))
            action = await _run_turn(d, turn, tt)
            if turn.unheard:
                unheard.append(ConversationOutput(turn.unheard, f"unheard:{turn.turn_id}",
                                                  next_action="listen", unheard=True))
            if turn.agent_started:
                unsubmitted.clear()
            elif getattr(transcript, "superseded", False):
                unsubmitted.append(str(transcript))
            else:
                unsubmitted.clear()  # explicit cancellation does not carry a request forward
            if action in ("standby", "mute"):  # either way the device stops sending audio
                _discard_after_signoff(session, deferred_outputs)
                getattr(session, "end_exchange", lambda: None)()
            await session.wait_for_playback_tail()
            if after_turn is not None and not session.stopped and not turn.tts_stop.is_set():
                await after_turn()
        except asyncio.CancelledError:
            tt.set_missing(outcome="interrupted", interrupted=True)
            raise
        except Exception:
            tt.set_missing(outcome="error")
            raise
        finally:
            _close_trace(session, tt, turn)


async def _send_user_transcript(d: _Driver, turn: _Turn) -> None:
    """The user's transcript frame: sent only when Hermes has stored the message, with its id and
    its text. A request that never became a message has none."""
    while not turn.agent_started and not turn.tts_stop.is_set():
        await asyncio.sleep(0.02)
    if not turn.agent_started:
        return
    found = await d.message_ids.user_message(turn.turn_id, turn.settled)
    if found is not None:
        turn.user_id, text = found
        await d.send_json({"type": "transcript", "message_id": turn.user_id, "role": "user",
                           "text": text or str(turn.transcript), **turn.meta})


async def _flush_user_transcript(turn: _Turn) -> None:
    frame, turn.user_frame = getattr(turn, "user_frame", None), None
    if frame is not None:
        await frame


async def _run_turn(d: _Driver, turn: _Turn, tt: Any) -> str:
    """Run one agent turn and speak its reply; return the explicit client next action."""
    session = d.session
    interrupted_in = session.take_interrupted()
    cancelled_before_start = turn.tts_stop.is_set()
    if cancelled_before_start:
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
    def interrupt(stop_run: bool = True) -> None:
        # Speech over an agent turn leaves its run going (Hermes decides what the words do to it,
        # as for a Telegram message typed mid-run); `cancel` and prepared replies just stop.
        if stop_run or d.release_turn is None or turn.output is not None or cancelled_before_start:
            d.loop.call_soon_threadsafe(turn_task.cancel)
            return
        turn.released = True
        d.loop.call_soon_threadsafe(d.release_turn, turn.pcm_chunks > 0)
    session.begin_turn(turn.turn_id, tts_stop=turn.tts_stop, on_interrupt=interrupt)
    d.turn = turn
    if turn.output is not None and (turn.output.next_action == "mute" or turn.output.no_barge_in):
        # A turn the server started called `mute` or `no_barge_in`: it holds from its reply's start.
        getattr(session, "ignore_microphone", lambda **kw: None)(muted=turn.output.next_action == "mute")
    progress_task = producer = None
    try:
        if not turn.tts_stop.is_set():
            await _send_state(d, turn, "thinking")
            await d.send_json(_thinking_frame(d, turn))
        # The TTS task carries the request's credential/profile context into each chunk's call.
        producer = asyncio.create_task(_produce_speech(d, turn))
        progress_task = asyncio.create_task(_report_thinking(d, turn))
        await _stream_reply(d, turn, turn_task)

        # The audio is already cut. Speech released the run (it goes on for the words to reach);
        # anything else that set `tts_stop` stops it through the host's hook, so `await turn_task`
        # below returns at once. (timed_out sets tts_stop too; that path already cancelled.)
        if turn.tts_stop.is_set() and not turn.timed_out and not turn.released and d.interrupt_turn is not None:
            try:
                d.interrupt_turn()
            except Exception:
                logger.debug("converse: interrupt_turn failed", exc_info=True)

        try:
            await turn_task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise  # the driver itself is being cancelled, not the (timed-out) turn task
        except Exception:
            pass  # recorded in turn.result by _run_agent
        # A cut turn is settled first, so whether its request became a message is known.
        if not turn.released:
            turn.settled.set()
        await _flush_user_transcript(turn)
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
        # keepalives may extend a client's next turn or outlive this driver. Nothing reads the
        # reply's audio any more, so its synthesis stops too.
        turn.settled.set()
        d.turn = None
        tasks = [task for task in (progress_task, producer) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    if turn.output:
        reply = turn.result.get("reply") or "".join(turn.reply_parts)
    else:
        reply = _record_history(d.history, turn)
    barged = bool(session.take_interrupted() or turn.tts_stop.is_set()) and not turn.timed_out
    turn_err = bool(turn.result.get("err"))
    final = (turn.result.get("reply") or "").strip()
    if barged and turn.released and not turn.pcm_chunks and final and final.upper() != "[SILENT]":
        # The run had finished, but none of its reply had played: it is delivered, as a
        # Telegram reply is, before the answer to what the person said.
        turn.unheard = final
    if barged:
        await d.send_json({"type": "interrupted", "turn_id": turn.turn_id})
    elif turn_err:
        await d.send_json({"type": "error", "error": turn.result["err"], "turn_id": turn.turn_id})
    # Only a completed turn may go to standby. Partial output from a failed or interrupted
    # run is not a decision to put the client in standby.
    completed = not (barged or turn_err or turn.timed_out)
    next_action = (
        "mute" if completed and d.mute is not None and d.mute()
        else "standby" if completed and d.standby is not None and d.standby()
        else "listen"
    )
    if completed and turn.output:
        choose_output = getattr(session, "output_next_action", None)
        next_action = (choose_output(turn.output.next_action) if choose_output is not None
                       else turn.output.next_action or "standby")
    if d.message_ids is not None and turn.shown and completed:
        # A server-started reply is a message too: find it by what it said.
        turn.assistant_id = await (d.message_ids.assistant_id(turn.turn_id) if turn.agent_started
                                   else d.message_ids.output_id(" ".join(turn.spoken_parts)))
    if d.transcripts and turn.shown and turn.assistant_id is not None:
        # The reply went out a piece at a time before its audio; this completes it.
        await d.send_json({"type": "transcript", "message_id": turn.assistant_id, "role": "assistant",
                           "turn_id": turn.turn_id, "text": ""})
    tt.set(**{"turn.attached": not d.gone()})  # a device was there when the reply was ready
    # Arm before turn_done so an immediate state report cannot be lost.
    if session.finish_turn(turn.turn_id, d.tail_fallback if turn.pcm_chunks else 0):
        next_action = "listen"
    record_action = getattr(session, "record_next_action", None)
    if record_action is not None:
        record_action(next_action)
    action_source = ("server_output" if turn.output is not None else
                     "standby_tool" if next_action == "standby" else
                     "mute_tool" if next_action == "mute" else "user_turn")
    # One line per turn that says which turn it was and why it was (or was not) heard. Lengths
    # only: never the transcript or reply text. reply_chars counts streamed deltas, final_chars the
    # run's final reply, spoken_chars what reached TTS.
    outcome = ("cancelled" if cancelled_before_start else "timed_out" if turn.timed_out
               else "barged" if barged else "error" if turn_err else "completed")
    logger.info(
        "converse timing: first_delta=%sms first_sentence=%sms first_pcm=%sms total=%sms "
        "deltas=%d reply_chars=%d session=%s turn=%s outcome=%s final_chars=%d spoken_chars=%d "
        "pcm_chunks=%d next_action=%s action_source=%s",
        turn.ms("first_delta"), turn.ms("first_sentence"), turn.ms("first_pcm"),
        turn.ms("pcm_done"), len(turn.reply_parts), sum(len(p) for p in turn.reply_parts),
        getattr(session, "session_id", None), turn.turn_id, outcome, len(turn.result.get("reply") or ""),
        sum(len(p) for p in turn.spoken_parts), turn.pcm_chunks, next_action, action_source)
    # Ordinary completion preserves the device's interaction mode. An explicit output
    # question can open listening; an ordinary or replayed reply never reopens a released PTT mic.
    turn_done: dict[str, Any] = {"type": "turn_done", "turn_id": turn.turn_id}
    if next_action in ("standby", "mute") or (
            turn.output is not None and turn.output.next_action == "listen" and not turn.output.unheard):
        turn_done["next_action"] = next_action
    await d.send_json(turn_done)
    _finish_trace(tt, turn, reply, err=turn_err, barged=barged, next_action=next_action)
    return next_action


def _thinking_frame(d: _Driver, turn: _Turn) -> dict[str, Any]:
    """Measured progress only: a run's counters are read once its agent turn has started."""
    run = turn.agent_started
    count = d.tool_calls() if run and d.tool_calls is not None else 0
    reasoning = d.reasoning_tokens() if run and d.reasoning_tokens is not None else None
    return {"type": "thinking", "tool_calls": count, "reasoning_tokens": reasoning, **turn.meta}


async def _send_state(d: _Driver, turn: _Turn, state: str) -> None:
    """Tell the client which state to show, when it changes."""
    if turn.state != state:
        turn.state = state
        await d.send_json({"type": "state", "state": state, **turn.meta})


async def _report_thinking(d: _Driver, turn: _Turn) -> None:
    """Repeat turn liveness and measured progress, including while streaming speech, and say when
    the turn starts and stops waiting on the person."""
    awaiting_answer = getattr(d.session, "awaiting_answer", None)
    next_frame = time.monotonic() + THINKING_FRAME_INTERVAL
    while True:
        await asyncio.sleep(max(0.0, min(STATE_POLL_SECONDS, next_frame - time.monotonic())))
        if turn.tts_stop.is_set():
            return
        if d.gone():  # nobody to tell; a new connection picks the turn up
            next_frame = max(next_frame, time.monotonic())
            continue
        waiting = bool(awaiting_answer()) if awaiting_answer is not None else False
        if waiting and turn.state != "listening":
            await _send_state(d, turn, "listening")
        elif not waiting and turn.state == "listening":
            await _send_state(d, turn, "thinking")
        if time.monotonic() >= next_frame:
            next_frame += THINKING_FRAME_INTERVAL
            await d.send_json(_thinking_frame(d, turn))


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


async def _reply_sentences(d: _Driver, turn: _Turn) -> AsyncIterator[str]:
    """Cut the streaming deltas into sentences as they land. A buffered tail is spoken after ~2 s
    without a new delta (at once if it already ends a sentence), unless it is an open <think>."""
    chunker = speech.sentence_chunker()
    idle_polls = 0
    epoch, taken = turn.speech_epoch, 0
    while not turn.tts_stop.is_set():
        try:
            delta = await turn.text_q.get_async(IDLE_POLL_SECONDS)
        except queue.Empty:
            delta = queue.Empty
        if epoch != turn.speech_epoch:  # a reconnect replayed what is buffered: it is not spoken
            chunker, epoch = speech.sentence_chunker(), turn.speech_epoch
        if delta is queue.Empty:
            idle_polls += 1
            if d.gone():
                continue
            buffered = chunker.buf.strip()
            if not buffered or ("<think" in chunker.buf and "</think>" not in chunker.buf):
                continue
            if buffered.endswith((".", "!", "?", "…", ":")) or idle_polls >= IDLE_POLLS_TO_FLUSH:
                for sentence in chunker.flush():
                    yield sentence
            continue
        idle_polls = 0
        if isinstance(delta, str):
            taken += 1
            if taken <= turn.replayed_deltas:  # received before the reconnect: replayed, not spoken
                continue
        if d.gone():  # nobody is listening: the text is in the conversation, not spoken
            if not isinstance(delta, Aside) and not turn.transient:
                turn.shown.append(chunker.buf + (delta or ""))
            chunker = speech.sentence_chunker()
            if delta is None:
                return
            continue
        if isinstance(delta, Aside):
            for sentence in chunker.flush():
                yield sentence
            yield delta
            continue
        if delta is None:
            for sentence in chunker.flush():
                yield sentence
            return
        for sentence in chunker.feed(delta):
            yield sentence


async def _produce_speech(d: _Driver, turn: _Turn) -> None:
    """The reply's TTS task: synthesize each sentence as it lands, so playback overlaps generation.
    Each chunk is a short blocking call; no thread is held between chunks."""
    spoken_chars = 0
    try:
        async for sentence in _reply_sentences(d, turn):
            epoch = turn.speech_epoch  # a reconnect after this replays the sentence instead
            cleaned = speech.strip_markdown(sentence)
            if not cleaned:
                continue
            shown = not (turn.transient or isinstance(sentence, Aside))
            turn.ns.setdefault("first_sentence", time.time_ns())
            for piece in split_text_for_tts_stream(cleaned, d.cap):
                if turn.tts_stop.is_set():
                    return
                if d.gone() or epoch != turn.speech_epoch:
                    break
                frame = None
                if shown:
                    text = piece if not turn.shown else " " + piece
                    turn.shown.append(text)
                    frame = {"type": "transcript", "role": "assistant", "turn_id": turn.turn_id, "text": text}
                turn.pcm_q.put_nowait(_Piece(epoch, frame))
                turn.spoken_parts.append(piece)
                getattr(d.session, "reply_text", lambda text: None)(piece)
                chunks = await asyncio.to_thread(lambda text=piece: iter(d.synth.synth(text)))
                while (chunk := await asyncio.to_thread(next, chunks, None)) is not None:
                    if turn.tts_stop.is_set():
                        return
                    if d.gone() or epoch != turn.speech_epoch:
                        break
                    turn.ns.setdefault("first_pcm", time.time_ns())
                    turn.pcm_q.put_nowait(chunk)
            spoken_chars += len(cleaned)
            if spoken_chars >= d.max_tts_chars:
                logger.debug("converse: TTS output capped at %d chars", spoken_chars)
                break
    except Exception as exc:
        logger.warning("converse synthesis failed: %s", exc, exc_info=True)
    finally:
        turn.pcm_q.put_nowait(None)


async def _stream_reply(d: _Driver, turn: _Turn, turn_task: asyncio.Future) -> None:
    """Stream the reply's PCM out; flip `playing` on only when real audio starts. A turn deadline
    backstops a hung turn: after `turn_timeout` with no progress at all the turn is abandoned, the
    task cancelled, and an error + turn_done still go out, so the client is never left "thinking"
    forever. Progress is reply text, audio, and whatever the host reports through `last_progress`
    (each tool call starting or finishing, each model call)."""
    speaking = False
    deadline = d.loop.time() + d.turn_timeout if d.turn_timeout > 0 else None

    def expired() -> bool:
        """Move the deadline to `turn_timeout` after the latest progress; True when that has passed."""
        nonlocal deadline
        latest = max(turn.progress_at, d.last_progress() if d.last_progress is not None else 0.0)
        deadline = max(deadline, latest + d.turn_timeout)
        return deadline <= d.loop.time()
    # Where the client's queued audio ends on this clock. Behind `now` means it ran dry (TTS was
    # slower than real time), so playback restarts at `now` rather than counting audio never played.
    rate = getattr(d.synth, "sample_rate", None)
    play_end = d.loop.time()
    while True:
        try:
            if deadline is None:
                chunk = await turn.pcm_q.get()
            else:
                if deadline <= d.loop.time() and expired():
                    turn.timed_out = True
                    break
                chunk = await asyncio.wait_for(turn.pcm_q.get(), timeout=deadline - d.loop.time())
        except TimeoutError:
            if expired():
                turn.timed_out = True
                break
            continue
        if deadline is not None:
            deadline = d.loop.time() + d.turn_timeout  # audio arriving is progress
        if chunk is None or turn.tts_stop.is_set():
            break
        await _flush_user_transcript(turn)
        if isinstance(chunk, _Piece):  # its transcript goes out in order ahead of the PCM it speaks
            # The contract: after a reconnect, audio starts with text prepared after it.
            turn.skipping = chunk.epoch != turn.speech_epoch
            if not turn.skipping and chunk.frame is not None and d.transcripts:
                await d.send_json(chunk.frame)
            continue
        if turn.skipping:
            continue
        if speaking and turn.respeak:
            turn.respeak = False
            await _send_state(d, turn, "speaking")
            await d.send_json({"type": "speaking", **turn.meta})
        if not speaking:
            turn.respeak = False
            d.session.set_playing(True, tts_stop=turn.tts_stop)
            if turn.tts_stop.is_set():
                d.session.set_playing(False)
                break
            await _send_state(d, turn, "speaking")
            await d.send_json({"type": "speaking", **turn.meta})
            speaking = True
        elif turn.state != "speaking":
            await _send_state(d, turn, "speaking")  # speech again after waiting on the person
        if turn.tts_stop.is_set():
            break
        if rate:
            # The first chunks go out at once; pacing starts only once the lead is reached.
            paced = d.loop.time()
            while play_end - d.loop.time() > MAX_AUDIO_LEAD_SECONDS and not turn.tts_stop.is_set():
                await asyncio.sleep(min(AUDIO_PACE_STEP_SECONDS, play_end - d.loop.time() - MAX_AUDIO_LEAD_SECONDS))
            if deadline is not None:
                deadline += d.loop.time() - paced  # waiting on playback is not a stalled run
            if turn.tts_stop.is_set():
                break
            play_start = max(play_end, d.loop.time())
            play_end = play_start + len(chunk) / 2 / rate
            getattr(d.session, "reply_audio", lambda *sent: None)(
                chunk, rate, play_start - d.loop.time() + time.monotonic())
            d.session.playback_until = play_end - d.loop.time() + time.monotonic()
        else:
            d.session.playback_until = math.inf
        turn.pcm_chunks += 1
        await d.send_bytes(chunk)
    turn.ns["pcm_done"] = time.time_ns()
    if turn.timed_out:
        turn.tts_stop.set()
        turn_task.cancel()
        turn.result.setdefault("err", f"voice turn timed out after {d.turn_timeout:.0f}s without progress")
        logger.warning("converse: turn abandoned after %.0fs without progress", d.turn_timeout)
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
    tt.set(outcome=outcome, interrupted=barged, next_action=next_action,
           **{"message.user_id": turn.user_id, "message.assistant_id": turn.assistant_id})


def _open_trace(session: Any, tt: Any, turn_id: str, source: str, claim_id: Optional[str] = None) -> None:
    """Name the turn's device, source and wake claim, and let the session find the live span."""
    tt.set(device_name=getattr(session, "device_name", None), claim_id=claim_id, **{"turn.source": source})
    bind = getattr(session, "bind_trace", None)
    if bind is not None:
        bind(turn_id, tt)


def _close_trace(session: Any, tt: Any, turn: _Turn) -> None:
    """Add what the session measured after the reply was sent (the drain and the barge-in totals),
    then end the turn. A turn whose device had already gone has neither."""
    try:
        tt.set_missing(**{"turn.attached": not session.stopped})
        take_tail = getattr(session, "take_tail_result", None)
        tail = take_tail(turn.turn_id) if take_tail is not None and turn.pcm_chunks else None
        sent_ns = turn.ns.get("pcm_done")
        if tail is not None and sent_ns is not None:
            reason, end_ns = tail
            tt.add_phase("voice.drain", sent_ns, end_ns, **{
                "drain.end": reason, "drain.wait_ms": round(max(0, end_ns - sent_ns) / 1e6)})
        take_barge = getattr(session, "take_barge_summary", None)
        if take_barge is not None:
            tt.set(**take_barge(turn.turn_id))
        bind = getattr(session, "bind_trace", None)
        if bind is not None:
            bind(None, None)
    except Exception:
        logger.debug("voice turn trace finish failed", exc_info=True)
    finally:
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
