"""The converse loop primitives: adaptive endpoint, session barge-in, the synthesizer seam and the
shared turn driver (control-frame ordering, history cap, session mode, timeouts, tracing)."""

from __future__ import annotations

import asyncio
import contextvars
import json
import queue as _queue
import threading
import types
import wave

import numpy as np
import pytest

from hermes_converse import loop as cl
from hermes_converse import speech, vad
from hermes_converse.loop import (
    AsyncQueue,
    HISTORY_MAX_MESSAGES, ConverseSession, QuietTick, TailBarge, Utterance,
    drive_converse_turns, parse_start_config, voice_system_prompt)
from hermes_converse.synth import decode_audio_file_to_pcm16, resample_synth, resolve_synthesizer

from tests.support.converse_audio import calibration, silence, speech_like


# ── endpointing ──

class _ScriptedDetector:
    def __init__(self, probs, on_call=None):
        self._probs = list(probs)
        self.threshold = 0.5
        self.calls = 0
        self._on_call = on_call

    def turn_complete_probability(self, audio_f32):
        p = self._probs[min(self.calls, len(self._probs) - 1)]
        self.calls += 1
        if self._on_call is not None:
            self._on_call(self.calls)
        return float(p)


def _adaptive_session(detector, *, endpoint_blocks=2, max_silence_blocks=8, max_blocks=500, transcribe=None):
    session = ConverseSession(np, transcribe=transcribe)  # endpoint_model off -> no model load
    session._turn_detector = detector
    if detector is not None:
        session._endpoint_threshold = detector.threshold
    session._endpoint_blocks = endpoint_blocks
    session._max_silence_blocks = max_silence_blocks
    session._max_blocks = max_blocks
    session._detector.is_quiet = lambda block: not np.any(block)  # zeros are quiet, anything else speech
    return session


def _feed_silence(session, blocks):
    silence_block = np.zeros(session._block, dtype=np.int16).tobytes()
    for _ in range(blocks):
        session.stream.feed(silence_block)


def _feed_speech(session, blocks):
    speech_block = np.full(session._block, 4000, dtype=np.int16).tobytes()
    for _ in range(blocks):
        session.stream.feed(speech_block)


async def _until(condition, timeout):
    """Wait on the loop until *condition()* holds; its value."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    return condition()


def _captured_blocks(session, wav_path):
    with wave.open(wav_path, "rb") as wf:
        frames = wf.getnframes()
    vad.unlink_quietly(wav_path)
    return frames // session._block


def test_adaptive_commits_immediately_when_turn_sounds_complete():
    det = _ScriptedDetector([0.95])
    session = _adaptive_session(det)
    _feed_silence(session, session._endpoint_blocks + 4)
    wav_path, volume = asyncio.run(session._capture())
    assert det.calls == 1
    assert volume >= 0
    assert _captured_blocks(session, wav_path) == session._endpoint_blocks + 4  # buffered audio is kept


def test_adaptive_rechecks_each_window_until_complete():
    det = _ScriptedDetector([0.1, 0.2, 0.9])
    session = _adaptive_session(det)
    _feed_silence(session, 20)
    wav_path, _volume = asyncio.run(session._capture())
    assert det.calls == 3


def test_adaptive_incomplete_turn_ends_at_max_silence():
    det = _ScriptedDetector([0.0])
    session = _adaptive_session(det, max_silence_blocks=9)
    _feed_silence(session, 20)
    wav_path, _volume = asyncio.run(session._capture())
    assert det.calls == 4  # checks at 2, 4, 6, 8 blocks of silence
    assert _captured_blocks(session, wav_path) == 9


def test_adaptive_resumed_speech_restores_the_full_pause_allowance():
    det = _ScriptedDetector([0.0, 0.0, 0.9])
    session = _adaptive_session(det, max_silence_blocks=6)
    _feed_speech(session, 2)
    _feed_silence(session, 5)  # an incomplete-sounding pause, bridged
    _feed_speech(session, 2)
    _feed_silence(session, 10)
    wav_path, _volume = asyncio.run(session._capture())
    assert det.calls == 3
    assert _captured_blocks(session, wav_path) == 2 + 5 + 2 + 10


def test_speech_during_inference_keeps_listening():
    session = None

    def arrives_during_inference(call):
        if call == 1:
            _feed_speech(session, 3)  # already queued when the model returns
            threading.Timer(0.2, _feed_silence, (session, 5)).start()

    det = _ScriptedDetector([0.9], on_call=arrives_during_inference)
    session = _adaptive_session(det)
    _feed_speech(session, 2)
    _feed_silence(session, 2)
    wav_path, _volume = asyncio.run(session._capture())
    assert det.calls == 2  # the complete verdict was discarded and the updated turn rechecked
    assert _captured_blocks(session, wav_path) == 2 + 2 + 3 + 5


def test_fixed_endpoint_ends_at_first_candidate_pause():
    session = _adaptive_session(None)
    _feed_speech(session, 3)
    _feed_silence(session, 10)
    wav_path, _volume = asyncio.run(session._capture())
    assert _captured_blocks(session, wav_path) == 3 + session._endpoint_blocks


def test_a_capture_at_the_cap_ends_at_its_last_pause_and_the_rest_is_heard_again():
    session = _adaptive_session(_ScriptedDetector([0.0]), endpoint_blocks=24, max_silence_blocks=100, max_blocks=60)
    pause = session._pause_blocks + 2
    _feed_speech(session, 30)
    _feed_silence(session, pause)
    _feed_speech(session, 40)
    wav_path, _volume = asyncio.run(session._capture())
    assert _captured_blocks(session, wav_path) == 30 + pause // 2  # ends in the middle of the pause
    rest = session.stream.ready_blocks(session._block)
    assert len(rest) == pause - pause // 2 + 40  # nothing lost, nothing heard twice
    assert not np.any(rest[0]) and np.any(rest[pause - pause // 2])


def test_a_capture_at_the_cap_with_no_pause_ends_at_the_cap():
    session = _adaptive_session(_ScriptedDetector([0.0]), endpoint_blocks=24, max_silence_blocks=100, max_blocks=60)
    short = session._pause_blocks - 1
    _feed_speech(session, 30)
    _feed_silence(session, short)  # too short to be a pause
    _feed_speech(session, 40)
    wav_path, _volume = asyncio.run(session._capture())
    assert _captured_blocks(session, wav_path) == 60
    assert len(session.stream.ready_blocks(session._block)) == 30 + short + 40 - 60


@pytest.mark.parametrize("smart_turn", [False, True])
def test_wake_pause_waits_for_the_request(smart_turn):
    det = _ScriptedDetector([0.9]) if smart_turn else None
    session = _adaptive_session(det)
    wake_word = session._wake_speech_blocks - 5
    _feed_speech(session, wake_word)  # "Hey Arya"
    _feed_silence(session, 6)  # checking that the device is listening
    _feed_speech(session, 20)  # "set a five minute timer"
    _feed_silence(session, 10)
    wav_path, _volume = asyncio.run(session._capture(wake=True))
    trailing = 10 if smart_turn else session._endpoint_blocks  # Smart Turn keeps buffered audio
    assert _captured_blocks(session, wav_path) == wake_word + 6 + 20 + trailing
    assert det is None or det.calls == 1


def test_wake_pause_is_bounded_by_max_silence():
    session = _adaptive_session(None)
    _feed_speech(session, 5)
    _feed_silence(session, 20)
    wav_path, _volume = asyncio.run(session._capture(wake=True))
    assert _captured_blocks(session, wav_path) == 5 + session._max_silence_blocks


def test_continuous_wake_request_endpoints_normally():
    session = _adaptive_session(None)
    _feed_speech(session, session._wake_speech_blocks + 10)
    _feed_silence(session, 10)
    wav_path, _volume = asyncio.run(session._capture(wake=True))
    assert _captured_blocks(session, wav_path) == session._wake_speech_blocks + 10 + session._endpoint_blocks


def test_without_wake_a_short_pause_ends_capture():
    session = _adaptive_session(None)
    _feed_speech(session, 5)
    _feed_silence(session, 6)
    _feed_speech(session, 20)
    wav_path, _volume = asyncio.run(session._capture())
    assert _captured_blocks(session, wav_path) == 5 + session._endpoint_blocks


def test_the_utterance_cap_is_a_memory_backstop_not_an_endpoint():
    """12 s truncated ordinary speech mid-sentence and silently lost the rest of the request.
    The endpointer decides where a turn ends; the cap only stops an unbounded capture."""
    assert vad.MAX_UTTERANCE_MS == 60_000
    assert ConverseSession(np)._max_blocks == 60_000 // 30


@pytest.mark.parametrize("wake", [False, True])
def test_client_commit_ends_capture_even_when_smart_turn_would_hold(wake):
    det = _ScriptedDetector([0.0])
    session = _adaptive_session(det)
    _feed_speech(session, 3)
    session.commit()
    wav_path, _volume = asyncio.run(session._capture(wake=wake))
    assert det.calls == 0
    assert _captured_blocks(session, wav_path) == 4


def test_endpointing_off_ignores_silence_and_smart_turn_until_commit():
    """The device ends the utterance, not the audio: 40 blocks of silence — far past
    ``max_silence_blocks`` — must not endpoint, and Smart Turn is never consulted."""
    det = _ScriptedDetector([0.99])  # would commit on the first ask
    session = _adaptive_session(det, endpoint_blocks=2, max_silence_blocks=4)
    session.set_endpointing(False)
    _feed_speech(session, 5)
    _feed_silence(session, 40)
    session.commit()
    wav_path, _volume = asyncio.run(session._capture())
    assert det.calls == 0
    assert _captured_blocks(session, wav_path) == 5 + 40 + 1  # the commit marker


def test_with_endpointing_the_same_silence_endpoints():
    """Control for the test above: the fixed/smart endpointer still ends an ordinary capture."""
    det = _ScriptedDetector([0.0])
    session = _adaptive_session(det, endpoint_blocks=2, max_silence_blocks=4)
    _feed_speech(session, 5)
    _feed_silence(session, 40)
    wav_path, _volume = asyncio.run(session._capture())
    assert _captured_blocks(session, wav_path) == 5 + 4


def test_endpointing_off_uses_the_same_utterance_cap():
    """Turning endpointing off changes the endpoint, not the capture's memory backstop."""
    session = ConverseSession(np)
    session.set_endpointing(False)
    assert session._max_blocks == vad.MAX_UTTERANCE_MS // 30


def test_endpointing_turned_on_with_commit_applies_after_the_held_audio():
    """Release sends ``on`` with ``commit``: the held silence already sent still cannot endpoint,
    and the next utterance endpoints normally."""
    session = _adaptive_session(_ScriptedDetector([0.0]), endpoint_blocks=2, max_silence_blocks=4)
    session.set_endpointing(False)
    _feed_speech(session, 5)
    _feed_silence(session, 40)
    session.set_endpointing(True, commit=True)
    assert not session._endpointing
    wav_path, _ = asyncio.run(session._capture())
    assert _captured_blocks(session, wav_path) == 5 + 40 + 1  # the commit marker
    assert session._endpointing
    _feed_speech(session, 5)
    _feed_silence(session, 40)
    wav_path, _ = asyncio.run(session._capture())
    assert _captured_blocks(session, wav_path) == 5 + 4


@pytest.mark.asyncio
async def test_commit_queued_with_speech_before_onset_still_transcribes():
    """PCM and commit can both reach the stream before the worker detects onset."""
    session = ConverseSession(np, transcribe=lambda path: "set a timer", capture_no_data_seconds=1.0)
    session.stream.feed(calibration().tobytes())
    session.stream.feed(speech_like(2000, seconds=1.0).tobytes())
    session.commit()  # no further PCM
    session.start()
    try:
        item = await session.transcripts.get_async(3)
        assert isinstance(item, Utterance) and item == "set a timer"
    finally:
        session.stop()
        await session._worker


def test_commit_arriving_during_inference_ends_capture_without_waiting():
    session = None

    def speech_and_commit_during_inference(call):
        if call == 1:
            _feed_speech(session, 3)
            session.commit()

    det = _ScriptedDetector([0.9], on_call=speech_and_commit_during_inference)
    session = _adaptive_session(det)
    session._capture_no_data_seconds = 1.0
    _feed_speech(session, 2)
    _feed_silence(session, 2)
    wav_path, _volume = asyncio.run(session._capture())
    assert not session.stream.no_data_return
    assert det.calls == 1
    assert _captured_blocks(session, wav_path) == 2 + 2 + 3 + 1
    assert session.stream.committed is False


def test_endpoint_windows_round_up_to_whole_blocks():
    session = ConverseSession(np, endpoint_silence_ms=700, endpoint_max_silence_ms=3000)
    assert session._endpoint_blocks * vad.BLOCK_MS == 720
    assert session._max_silence_blocks * vad.BLOCK_MS == 3000


def test_frames_to_f32_resamples_non_16k_capture():
    session = ConverseSession(np, input_rate=48000)
    frames = [np.full(session._block, 4000, dtype=np.int16) for _ in range(10)]
    out = session._frames_to_f32_16k(frames)
    assert out.dtype == np.float32
    assert abs(out.size - round(session._block * 10 * 16000 / 48000)) <= 1
    assert float(np.max(np.abs(out))) <= 1.0


def test_capture_and_transcribe_returns_transcript_and_unlinks_wav(tmp_path):
    seen = {}

    def _transcribe(path):
        seen["path"] = path
        return "  hello there  "

    session = _adaptive_session(_ScriptedDetector([0.9]), transcribe=_transcribe)
    _feed_silence(session, session._endpoint_blocks + 4)
    transcript, volume = asyncio.run(session._capture_and_transcribe())
    assert transcript == "hello there"
    assert volume >= 0
    import os
    assert not os.path.exists(seen["path"])


def test_transcribe_failure_is_nothing_heard():
    def _boom(path):
        raise RuntimeError("provider down")

    session = _adaptive_session(_ScriptedDetector([0.9]), transcribe=_boom)
    _feed_silence(session, session._endpoint_blocks + 4)
    transcript, _volume = asyncio.run(session._capture_and_transcribe())
    assert transcript == ""


# ── ConverseSession barge-in / playing flag ──

def test_converse_session_barge_in_sets_interrupt_and_stops_tts():
    session = ConverseSession(np)
    tts_stop = threading.Event()
    session.set_playing(True, tts_stop=tts_stop)
    assert session.playing() is True
    session._trigger_barge_in()
    assert tts_stop.is_set()
    assert session.take_interrupted() is True
    assert session.take_interrupted() is False
    assert session.playing() is False


def test_converse_session_stop_pushes_sentinel():
    session = ConverseSession(np)
    session.stop()
    assert session.stopped is True
    assert session.transcripts.get_nowait() is None


def test_playback_tail_suppresses_quiet_until_drained():
    session = ConverseSession(np, quiet_interval=0.5)

    def _drain_ticks():
        out = []
        while True:
            try:
                out.append(session.transcripts.get_nowait())
            except Exception:
                break
        return [t for t in out if isinstance(t, QuietTick)]

    session.begin_playback_tail("t1", fallback_seconds=100)
    for _ in range(50):
        session._account_received_silence()
    assert _drain_ticks() == []
    session.playback_ended("other")
    for _ in range(50):
        session._account_received_silence()
    assert _drain_ticks() == []
    session.playback_ended("t1")
    for _ in range(50):
        session._account_received_silence()
    assert len(_drain_ticks()) >= 1


@pytest.mark.asyncio
async def test_first_quiet_tick_after_resume_is_a_full_interval_later():
    """Silence received before the client stopped sending must not shorten the next listening phase."""
    import time

    session = ConverseSession(np, quiet_interval=0.3)  # 10 blocks
    session.stream.resume_gap_seconds = 0.1
    session.start()
    try:
        async def ticks_after(blocks):
            _feed_silence(session, blocks)
            deadline = time.monotonic() + 2
            while session.stream._chunks and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            out = []
            while True:
                try:
                    out.append(session.transcripts.get_nowait())
                except _queue.Empty:
                    return [t for t in out if isinstance(t, QuietTick)]

        assert await ticks_after(8) == []  # an earlier listening phase: 0.24 s of silence, then the client stops
        await asyncio.sleep(0.2)  # client stopped sending; nothing received
        assert await ticks_after(3) == []  # resumed: 0.09 s is not an interval
        ticks = await ticks_after(7)
        assert len(ticks) == 1 and ticks[0].quiet_seconds == 0.3
    finally:
        session.stop()
        await session._worker


def test_split_sample_resume_requires_a_full_new_quiet_interval(monkeypatch):
    """Drive the real worker synchronously so each assertion follows a processed audio block."""
    session = ConverseSession(np, quiet_interval=0.3)
    read = session.stream.read_blocks
    silence_block = np.zeros(session._block, dtype=np.int16).tobytes()
    blocks = 0
    completed = False

    async def feed_then_read(block, **kwargs):
        nonlocal blocks, completed
        # Eight old blocks = 0.24 s; nine new blocks = 0.27 s, still below the interval.
        if blocks in (8, 11, 17):
            assert session.transcripts.empty()
        if blocks == 18:
            tick = session.transcripts.get_nowait()
            assert isinstance(tick, QuietTick) and tick.quiet_seconds == 0.3
            assert session.transcripts.empty()
            completed = True
            session._stop.set()
            return [np.zeros(block, dtype=np.int16)]
        if blocks == 8:
            session.stream._last_feed -= 1.1  # a receive gap above the real 1 s threshold
            session.stream.feed(silence_block[:1])
            session.stream.feed(silence_block[1:])
        else:
            session.stream.feed(silence_block)
        blocks += 1
        return await read(block, **kwargs)

    monkeypatch.setattr(session.stream, "read_blocks", feed_then_read)
    asyncio.run(session._run())
    assert completed and blocks == 18  # the worker logs exceptions instead of propagating them


# ── start frame + prompt ──

def test_parse_start_config_defaults_and_clamps():
    assert parse_start_config({}) == (16000, 24000, 0.0)
    assert parse_start_config({"input_rate": 100, "output_rate": "99000",
                               "quiet_interval": "15"}) == (8000, 48000, 15.0)
    assert parse_start_config({"quiet_interval": -3})[2] == 0.0


def test_voice_system_prompt_names_the_assistant():
    assert "Your name is Arya" in voice_system_prompt("Arya")
    assert "Your name" not in voice_system_prompt(None)


# ── converse synthesizer ──

def _write_wav(path, *, rate=24000, ms=50, freq=440.0):
    n = int(rate * ms / 1000)
    t = np.arange(n, dtype=np.float64) / rate
    samples = (np.sin(2 * np.pi * freq * t) * 12000).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())
    return n


def test_decode_audio_file_to_pcm16_roundtrips_wav(tmp_path):
    wav = tmp_path / "tone.wav"
    n = _write_wav(wav, rate=24000, ms=50)
    pcm = decode_audio_file_to_pcm16(str(wav), target_rate=24000)
    assert pcm and len(pcm) % 2 == 0
    assert abs(len(pcm) // 2 - n) <= max(64, n // 10)


def test_decode_audio_file_to_pcm16_bad_file_returns_empty(tmp_path):
    bad = tmp_path / "not-audio.bin"
    bad.write_bytes(b"not an audio file")
    assert decode_audio_file_to_pcm16(str(bad)) == b""


def test_resolve_synthesizer_uses_streaming_when_available(monkeypatch):
    class _FakeStreamer:
        sample_rate = 24000

        def stream(self, text):
            yield b"\x01\x02\x03\x04"

    monkeypatch.setattr(speech, "streaming_provider", lambda cfg: _FakeStreamer())
    synth = resolve_synthesizer({})
    assert synth.sample_rate == 24000
    assert list(synth.synth("hi")) == [b"\x01\x02\x03\x04"]


def test_resolve_synthesizer_falls_back_to_one_shot(monkeypatch, tmp_path):
    monkeypatch.setattr(speech, "streaming_provider", lambda cfg: None)
    wav = tmp_path / "reply.wav"
    _write_wav(wav, rate=24000, ms=40)
    monkeypatch.setattr(speech, "text_to_speech", lambda text: json.dumps({"success": True, "file_path": str(wav)}))
    synth = resolve_synthesizer({})
    assert synth.sample_rate == 24000
    pcm = b"".join(synth.synth("say something"))
    assert pcm and len(pcm) % 2 == 0
    assert not wav.exists()  # the temp file was unlinked after synthesis


def test_one_shot_fallback_yields_nothing_on_provider_failure(monkeypatch):
    monkeypatch.setattr(speech, "streaming_provider", lambda cfg: None)
    monkeypatch.setattr(speech, "text_to_speech", lambda text: json.dumps({"success": False, "error": "nope"}))
    assert list(resolve_synthesizer({}).synth("x")) == []


class _ToneSynth:
    def __init__(self, sample_rate, n_samples=4800):
        self.sample_rate = sample_rate
        self._n = n_samples

    def synth(self, text):
        t = np.arange(self._n, dtype=np.float64) / self.sample_rate
        pcm = (np.sin(2 * np.pi * 220.0 * t) * 10000).astype(np.int16).tobytes()
        yield pcm[:1001]  # an odd byte boundary exercises the carry
        yield pcm[1001:]


def test_resample_synth_is_noop_when_rates_match():
    inner = _ToneSynth(24000)
    assert resample_synth(inner, 24000) is inner


def test_resample_synth_changes_byte_length_at_new_rate():
    inner = _ToneSynth(24000, n_samples=4800)
    src_bytes = len(b"".join(inner.synth("hi")))
    wrapped = resample_synth(inner, 12000)
    assert wrapped.sample_rate == 12000
    out_bytes = len(b"".join(wrapped.synth("hi")))
    assert out_bytes % 2 == 0
    assert abs(out_bytes - src_bytes // 2) <= max(256, src_bytes // 10)
    assert out_bytes < src_bytes


def test_resample_synth_upsamples_to_more_bytes():
    inner = _ToneSynth(16000, n_samples=3200)
    src_bytes = len(b"".join(inner.synth("hi")))
    assert len(b"".join(resample_synth(inner, 24000).synth("hi"))) > src_bytes


# ── shared turn driver ──

class _FakeConverseSession:
    """Serves fixed transcripts (then the shutdown sentinel) with no VAD."""

    def __init__(self, transcripts):
        self.transcripts = AsyncQueue()
        for t in transcripts:
            self.transcripts.put(t)
        self.transcripts.put(None)
        self.stopped = False
        self.turns_begun = self.turns_ended = self.tails_begun = 0

    def enable_turn_control(self):
        pass

    def capture_pending(self):
        return False

    def reserve_turn(self, turn_id, tts_stop, utterance=None):
        pass

    def release_capture(self):
        pass

    async def wait_for_playback_tail(self):
        pass

    def take_interrupted(self):
        return False

    def set_playing(self, value, *, tts_stop=None):
        return None

    def begin_turn(self, turn_id=None, **kwargs):
        self.turns_begun += 1

    def end_turn(self):
        self.turns_ended += 1

    def finish_turn(self, turn_id, playback_tail_seconds):
        self.end_turn()
        if playback_tail_seconds > 0:
            self.begin_playback_tail(turn_id, playback_tail_seconds)

    def begin_playback_tail(self, turn_id, fallback_seconds):
        self.tails_begun += 1

    def end_playback_tail(self):
        pass

    def playback_ended(self, turn_id=None):
        pass


class _EchoSynth:
    sample_rate = 24000

    def synth(self, text):
        yield text.encode("utf-8")


def _run_driver(session, history, replies, quiet_interval=0.0, interrupt_turn=None, **kw):
    sent: list = []

    async def _send_json(obj):
        sent.append(obj)

    async def _send_bytes(data):
        sent.append(("bytes", data))

    reply_iter = iter(replies)

    async def _run_turn(transcript, on_delta, *, interrupted):
        reply = next(reply_iter)
        on_delta(reply)
        return reply, None

    async def _main():
        await drive_converse_turns(
            session=session, synth=_EchoSynth(), cap=4000, loop=asyncio.get_running_loop(),
            send_json=_send_json, send_bytes=_send_bytes, run_turn=_run_turn, history=history,
            quiet_interval=quiet_interval, interrupt_turn=interrupt_turn, **kw)

    asyncio.run(_main())
    return sent


def _types(sent):
    return [f.get("type") if isinstance(f, dict) else "bytes" for f in sent]


def _turn_done_frame(sent):
    frame = next(f for f in sent if isinstance(f, dict) and f.get("type") == "turn_done")
    assert set(frame) in ({"type", "turn_id"}, {"type", "turn_id", "next_action"})
    if "next_action" in frame:
        assert frame["next_action"] == "standby"
    return frame


def _spoken_bytes(sent):
    return b"".join(f[1] for f in sent if isinstance(f, tuple) and f[0] == "bytes")


def test_driver_barge_in_interrupts_the_in_flight_turn():
    class _BargingSession(_FakeConverseSession):
        def set_playing(self, value, *, tts_stop=None):
            if value and tts_stop is not None:
                tts_stop.set()  # the user spoke over the reply the moment it began playing

    calls = []
    sent = _run_driver(_BargingSession(["turn it on"]), [], ["Sure, turning it — "], quiet_interval=1.0,
                       interrupt_turn=lambda: calls.append(True))
    assert calls == [True]
    assert _spoken_bytes(sent) == b""  # cancellation also discards already queued PCM
    assert "interrupted" in _types(sent) and _types(sent)[-1] == "turn_done"


@pytest.mark.parametrize("stop_run", [False, True])
def test_driver_speech_barge_in_releases_the_run_and_cancel_stops_it(stop_run):
    """Speech leaves the agent run going for the words to reach (``release_turn``); ``cancel``
    stops it (``interrupt_turn``)."""
    class _BargingSession(_FakeConverseSession):
        def begin_turn(self, turn_id=None, *, tts_stop=None, on_interrupt=None):
            super().begin_turn(turn_id)
            self.tts_stop, self.on_interrupt = tts_stop, on_interrupt

        def set_playing(self, value, *, tts_stop=None):
            if value and self.on_interrupt is not None:
                self.on_interrupt(stop_run)  # heard the moment the reply began playing
                self.tts_stop.set()

    stopped, released = [], []
    sent = _run_driver(_BargingSession(["turn it on"]), [], ["Sure, turning it — "], quiet_interval=1.0,
                       interrupt_turn=lambda: stopped.append(True), release_turn=released.append)
    assert (stopped, released) == (([], [False]) if not stop_run else ([True], []))
    assert "interrupted" in _types(sent) and _types(sent)[-1] == "turn_done"


def test_driver_no_interrupt_on_normal_completion():
    calls = []
    _run_driver(_FakeConverseSession(["what time is it"]), [], ["It's noon."], quiet_interval=1.0,
                interrupt_turn=lambda: calls.append(True))
    assert calls == []


def test_drive_converse_turns_control_frame_ordering():
    session = _FakeConverseSession(["hello there."])
    history: list = []
    sent = _run_driver(session, history, ["Hi back."], transcripts=True)
    # The assistant's text goes out ahead of the audio that speaks it.
    assert _types(sent) == ["transcript", "state", "thinking", "transcript", "state", "speaking", "bytes",
                           "turn_done"]
    assert sent[0]["role"] == "user" and sent[0]["text"] == "hello there."
    assert sent[6] == ("bytes", b"Hi back.")
    assert sent[3]["role"] == "assistant" and sent[3]["text"] == "Hi back."
    assert [f["state"] for f in sent if isinstance(f, dict) and f["type"] == "state"] == ["thinking", "speaking"]
    turn_ids = {f["turn_id"] for f in sent if isinstance(f, dict) and "turn_id" in f}
    assert len(turn_ids) == 1 and sent[0]["turn_id"] == sent[-1]["turn_id"]
    thinking = next(f for f in sent if isinstance(f, dict) and f.get("type") == "thinking")
    assert thinking["turn_id"] == sent[0]["turn_id"]
    assert history == [{"role": "user", "content": "hello there."}, {"role": "assistant", "content": "Hi back."}]


def test_drive_converse_turns_caps_history_tail():
    n_turns = HISTORY_MAX_MESSAGES
    history: list = []
    _run_driver(_FakeConverseSession([f"say {i}." for i in range(n_turns)]), history,
                [f"reply {i}." for i in range(n_turns)])
    assert len(history) == HISTORY_MAX_MESSAGES
    assert history[-1] == {"role": "assistant", "content": f"reply {n_turns - 1}."}
    assert history[0]["role"] == "user"


def test_drive_converse_turns_brackets_the_turn_with_begin_end():
    session = _FakeConverseSession(["hello there."])
    _run_driver(session, [], ["Hi back."])
    assert session.turns_begun == 1 and session.turns_ended == 1


def test_drive_converse_turns_forwards_quiet_ticks_and_keeps_socket_open():
    session = _FakeConverseSession([])
    session.transcripts = AsyncQueue()
    for q in (0.5, 1.0, 1.5):
        session.transcripts.put(QuietTick(q))
    session.transcripts.put(None)
    sent = _run_driver(session, [], [], quiet_interval=0.5)
    quiets = [f for f in sent if isinstance(f, dict) and f.get("type") == "quiet"]
    assert [f["quiet_seconds"] for f in quiets] == [0.5, 1.0, 1.5]
    assert "turn_done" not in _types(sent)


@pytest.mark.parametrize("words", ["stop", "cancel"])
def test_stop_words_reach_agent(words):
    history = []
    sent = _run_driver(_FakeConverseSession([words]), history, ["Okay."])
    assert "thinking" in _types(sent)
    assert history[0]["content"] == words


def test_drive_converse_turns_times_out_hung_turn():
    session = _FakeConverseSession(["are you there?"])
    sent: list = []

    async def _sj(o):
        sent.append(o)

    async def _sb(d):
        sent.append(("bytes", d))

    cancelled = []

    async def _hang(transcript, on_delta, *, interrupted):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return "", None

    async def _main():
        await drive_converse_turns(
            session=session, synth=_EchoSynth(), cap=4000, loop=asyncio.get_running_loop(),
            send_json=_sj, send_bytes=_sb, run_turn=_hang, history=[], turn_timeout=0.3)

    asyncio.run(_main())
    assert "error" in _types(sent) and _types(sent)[-1] == "turn_done"
    assert "next_action" not in _turn_done_frame(sent)
    assert cancelled == [True]  # the hung turn was cancelled, not left running


def test_stt_and_tts_threads_run_in_the_spawners_context(monkeypatch):
    probe = contextvars.ContextVar("probe", default=None)
    seen = {}

    def _transcribe(path, **kw):
        seen["stt"] = probe.get()
        return "hello"

    monkeypatch.setattr(speech, "transcribe_wav", _transcribe)
    probe.set("request-scope")
    async def _listen():
        session = ConverseSession(np)
        session.start()
        try:
            for pcm in (calibration(), speech_like(2000, seed=1), silence(seconds=1.8)):
                session.stream.feed(pcm.tobytes())
            assert await session.transcripts.get_async(6) == "hello"
        finally:
            session.stop()
            await session._worker

    asyncio.run(_listen())

    class _ProbeSynth(_EchoSynth):
        def synth(self, text):
            seen["tts"] = probe.get()
            yield from super().synth(text)

    def _driver_in_scope():
        async def _send(_):
            return None

        async def _run_turn(transcript, on_delta, *, interrupted):
            on_delta("Done.")
            return "Done.", None

        async def _main():
            await drive_converse_turns(
                session=_FakeConverseSession(["go"]), synth=_ProbeSynth(), cap=4000,
                loop=asyncio.get_running_loop(), send_json=_send, send_bytes=_send,
                run_turn=_run_turn, history=[])

        asyncio.run(_main())

    contextvars.copy_context().run(_driver_in_scope)
    assert seen == {"stt": "request-scope", "tts": "request-scope"}


def test_drive_converse_turns_flushes_queue_on_signoff():
    session = _FakeConverseSession(["are we done?", "a queued leftover"])
    sent = _run_driver(session, [], ["All set."], quiet_interval=1.0, standby=lambda: True,
                       transcripts=True)
    transcripts = [(f["role"], f["text"]) for f in sent
                   if isinstance(f, dict) and f.get("type") == "transcript"]
    assert transcripts == [("user", "are we done?"), ("assistant", "All set.")]
    turn_dones = [f for f in sent if isinstance(f, dict) and f.get("type") == "turn_done"]
    assert len(turn_dones) == 1 and turn_dones[0]["next_action"] == "standby"


def test_driver_hands_each_turn_its_own_recorder(monkeypatch):
    class _Rec:
        def __init__(self, name):
            self.name, self.attrs = name, {}

        def set(self, **attrs):
            self.attrs.update(attrs)

    class _Turn:
        traceparent = None

        def add_phase(self, *a, **k):
            pass

        def set(self, **k):
            pass

        def end(self):
            pass

    opened, emitted = [], []

    def _start_turn(*, recorder, turn_id, **attrs):
        opened.append(recorder.name)
        return _Turn()

    monkeypatch.setattr(cl, "start_turn", _start_turn)
    monkeypatch.setattr(cl, "emit_turn_trace", emitted.append)
    r1, r2, r3 = _Rec("r1"), _Rec("r2"), _Rec("r3")
    session = _FakeConverseSession([Utterance("first", r1), Utterance("are we done?", r2), Utterance("leftover", r3)])
    _run_driver(session, [], ["ok.", "All set."], quiet_interval=1.0, standby=lambda: len(opened) > 1)
    assert opened == ["r1", "r2"]
    assert emitted == [r3] and r3.attrs == {"outcome": "discarded_after_signoff"}


def test_driver_tail_barge_emits_interrupted_not_a_turn():
    session = _FakeConverseSession([])
    session.transcripts = AsyncQueue()
    session.transcripts.put(TailBarge("turn-x"))
    session.transcripts.put(None)
    sent = _run_driver(session, [], [])
    assert sent == [{"type": "interrupted", "turn_id": "turn-x"},
                    {"type": "turn_done", "turn_id": "turn-x"}]
    assert session.turns_begun == 0


def test_drive_converse_turns_standby_tool_requests_standby():
    sent = _run_driver(_FakeConverseSession(["are we done?"]), [], ["All set."], quiet_interval=1.0, standby=lambda: True)
    assert _turn_done_frame(sent)["next_action"] == "standby"


def test_drive_converse_turns_question_preserves_client_mode():
    sent = _run_driver(_FakeConverseSession(["set a timer"]), [], ["Sure — for how long?"], quiet_interval=1.0)
    assert "next_action" not in _turn_done_frame(sent)


def test_drive_converse_turns_plain_reply_preserves_client_mode():
    sent = _run_driver(_FakeConverseSession(["what's the weather?"]), [], ["It's sunny and 72."], quiet_interval=1.0)
    assert "next_action" not in _turn_done_frame(sent)


def test_drive_converse_turns_caps_tts_output():
    long_reply = "This is a spoken sentence. " * 120  # ~3200 chars, well over the cap
    history: list = []
    sent = _run_driver(_FakeConverseSession(["say a lot."]), history, [long_reply], max_tts_chars=300)
    pcm = _spoken_bytes(sent)
    assert len(pcm) < len(long_reply) and len(pcm) <= 300 + 200
    assert history[-1] == {"role": "assistant", "content": long_reply}


def test_drive_converse_turns_reports_turn_error():
    sent: list = []

    async def _sj(o):
        sent.append(o)

    async def _sb(d):
        sent.append(("bytes", d))

    async def _failing(transcript, on_delta, *, interrupted):
        return "", "agent API refused the turn (409): permission_denied"

    async def _main():
        await drive_converse_turns(
            session=_FakeConverseSession(["hi"]), synth=_EchoSynth(), cap=4000,
            loop=asyncio.get_running_loop(), send_json=_sj, send_bytes=_sb, run_turn=_failing, history=[])

    asyncio.run(_main())
    errors = [f for f in sent if isinstance(f, dict) and f.get("type") == "error"]
    assert errors and "409" in errors[0]["error"] and _types(sent)[-1] == "turn_done"
    assert "next_action" not in _turn_done_frame(sent)


# ── per-turn cancel that keeps the session (issue #5) ──


def test_session_cancel_stops_matching_turn_without_creating_another_turn():
    session = ConverseSession(np)
    tts_stop = threading.Event()
    session.begin_turn("turn-abc", tts_stop=tts_stop)
    session.set_playing(True, tts_stop=tts_stop)
    assert session.cancel("turn-abc")
    assert tts_stop.is_set() and session.take_interrupted() is True
    assert not session.playing()
    assert session.transcripts.empty()


def test_session_cancel_idle_does_not_queue_marker():
    # Nothing playing and nothing capturing: cancel aborts nothing and emits nothing.
    session = ConverseSession(np)
    session.cancel()
    assert session._cancelled.is_set()
    assert session.transcripts.qsize() == 0


def test_session_cancel_discards_captured_transcript():
    seen = {}

    def _transcribe(path):
        seen["path"] = path
        return "heard it"

    session = ConverseSession(np, transcribe=_transcribe)
    session._cancelled.set()  # cancel arrived mid-capture
    session._capturing = True
    # Endpoint immediately with silence so capture returns.
    _feed_silence(session, session._endpoint_blocks + 4)
    # Replicate _run's post-capture discard: cancelled -> transcript not queued.
    transcript, volume = asyncio.run(session._capture_and_transcribe())
    assert volume >= 0
    assert transcript == "" and seen == {}  # cancelled capture never reaches STT
    assert session._cancelled.is_set()
    session._cancelled.clear()
    assert session.transcripts.qsize() == 0  # ... it was discarded, no turn ran


def test_driver_cancel_turn_emits_interrupted_and_turn_done():
    session = _FakeConverseSession([])
    sent = _run_driver(session, [], [], quiet_interval=1.0)
    # No cancel marker queued -> nothing sent.
    assert sent == []


def test_session_idle_cancel_does_not_poison_next_capture():
    # Cancel while idle sets _cancelled, but the next capture's transcript must still be queued
    # (cancel only discards the capture it interrupted).
    session = ConverseSession(np)
    session.cancel()  # idle: nothing playing, nothing capturing
    assert session._cancelled.is_set()
    # Simulate _run clearing _cancelled at capture start, then capture completes.
    session._cancelled.clear()
    session.transcripts.put("a fresh transcript")
    assert session.transcripts.get_nowait() == "a fresh transcript"


def test_driver_cancel_turn_emits_frames():
    session = _FakeConverseSession([])
    session.transcripts = AsyncQueue()
    session.transcripts.put(cl.CancelTurn("turn-9"))
    session.transcripts.put(None)
    sent = _run_driver(session, [], [], quiet_interval=1.0)
    assert _types(sent) == ["interrupted", "turn_done"]
    assert sent[0]["turn_id"] == "turn-9" and sent[1]["turn_id"] == "turn-9"
    assert "next_action" not in _turn_done_frame(sent)
    assert session.turns_begun == 0 and session.turns_ended == 0


@pytest.mark.parametrize("interrupted", [False, True])
def test_incomplete_signoff_keeps_listening_and_does_not_drop_queued_input(interrupted):
    class Session(_FakeConverseSession):
        def set_playing(self, value, *, tts_stop=None):
            if interrupted and value and tts_stop is not None:
                tts_stop.set()

    session = Session(["bye", "fresh request"])
    sent = []

    async def send(frame):
        sent.append(frame)

    async def run(transcript, on_delta, **kwargs):
        if transcript == "bye":
            on_delta("Goodbye. Over and out.")
            return "Goodbye. Over and out.", None if interrupted else "provider failed"
        on_delta("Ready.")
        return "Ready.", None

    async def main():
        await drive_converse_turns(
            session=session, synth=_EchoSynth(), cap=4000, loop=asyncio.get_running_loop(),
            send_json=send, send_bytes=send, run_turn=run, history=[], quiet_interval=1.0,
            standby=lambda: "Ready." not in str(sent))

    asyncio.run(main())
    completions = [frame for frame in sent if isinstance(frame, dict) and frame.get("type") == "turn_done"]
    assert len(completions) == 2  # the partial sign-off must not flush the next request
    assert all("next_action" not in frame for frame in completions)


def test_standby_preserves_playback_tail_until_matching_drain():
    session = ConverseSession(np)
    session.transcripts.put("bye")
    session.transcripts.put(None)
    async def scenario():
        sent = []
        async def send(frame):
            sent.append(frame)
            if isinstance(frame, dict) and frame.get("type") == "turn_done":
                assert session._tail_active
                session.playback_ended("another-turn")
                assert session._tail_active
                session.playback_ended(frame["turn_id"])
        async def run(text, on_delta, **kwargs):
            on_delta("Goodbye. Over and out.")
            return "Goodbye. Over and out.", None
        await drive_converse_turns(session=session, synth=_EchoSynth(), cap=4000,
            loop=asyncio.get_running_loop(), send_json=send, send_bytes=send,
            run_turn=run, history=[], quiet_interval=1.0, standby=lambda: True)
        return sent
    done = _turn_done_frame(asyncio.run(scenario()))
    assert done["next_action"] == "standby" and not session._tail_active


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['thinking', 'speaking', 'tail'])
async def test_accepted_barge_in_is_captured_in_every_phase(monkeypatch, phase):
    calls = []
    session = ConverseSession(np, transcribe=lambda path: calls.append(path) or 'continued request')
    stop = threading.Event()
    interrupted = threading.Event()
    session.begin_turn('active', tts_stop=stop, on_interrupt=lambda _stop_run: interrupted.set())
    if phase == 'speaking':
        session.set_playing(True, tts_stop=stop)
    elif phase == 'tail':
        session.end_turn()
        session.begin_playback_tail('active', 5)
        session.on_tail_barge = lambda turn_id: interrupted.set()
    session.start()
    try:
        for pcm in (calibration(), speech_like(5000, seed=23), silence(1.8)):
            session.stream.feed(pcm.tobytes())
        assert await _until(interrupted.is_set, 3)
        assert await session.transcripts.get_async(3) == 'continued request'
        assert len(calls) == 1
        if phase != 'tail':
            assert stop.is_set()
    finally:
        session.stop()
        await session._worker


@pytest.mark.asyncio
async def test_talk_past_the_cap_is_split_at_a_pause_with_nothing_lost():
    lengths = []

    def transcribe(path):
        with wave.open(path, 'rb') as wav:
            lengths.append(wav.getnframes() / wav.getframerate())
        return 'part'

    session = ConverseSession(np, transcribe=transcribe)
    session._max_blocks = 10_000 // vad.BLOCK_MS  # a 10 s cap keeps the test short
    session.start()
    try:
        for pcm in (calibration(), speech_like(5000, seconds=8, seed=31), silence(0.45),
                    speech_like(5000, seconds=8, seed=32), silence(1.8)):
            session.stream.feed(pcm.tobytes())
        assert await session.transcripts.get_async(5) == 'part'
        assert await session.transcripts.get_async(5) == 'part'
        with pytest.raises(_queue.Empty):
            await session.transcripts.get_async(1)
        first, second = lengths
        assert 8 < first < 9.9  # ended at the pause after the first 8 s, not at the cap
        assert first + second >= 16.4  # both stretches of speech and the pause between them
    finally:
        session.stop()
        await session._worker


def test_stale_cancel_does_not_touch_newer_thinking_turn():
    session = ConverseSession(np)
    stop = threading.Event()
    interrupted = []
    session.begin_turn('newer', tts_stop=stop, on_interrupt=lambda _stop_run: interrupted.append(True))
    assert not session.cancel('older')
    assert not stop.is_set() and interrupted == []
    assert session.cancel('newer')
    assert stop.is_set() and interrupted == [True]
    session.cancel('newer')
    assert interrupted == [True]


def test_thinking_completion_keeps_candidate_input_before_onset():
    session = ConverseSession(np)
    session.begin_turn('turn')
    session._pre_roll.append(np.full(480, 11, np.int16))
    session.stream.feed(np.full(480, 99, np.int16).tobytes())
    session.end_turn()
    (audio,) = asyncio.run(session.stream.read_blocks(480))
    assert np.all(audio == 99)
    assert np.all(session._pre_roll[0] == 11)


@pytest.mark.asyncio
async def test_continuation_and_later_fresh_input_are_both_preserved(monkeypatch):
    called = []
    tripped = threading.Event()
    session = ConverseSession(np, transcribe=lambda path: called.append(path) or 'fresh request')
    session.begin_turn('busy', tts_stop=threading.Event(), on_interrupt=lambda _stop_run: tripped.set())
    session.start()
    try:
        for pcm in (calibration(), speech_like(5000, seed=4)):
            session.stream.feed(pcm.tobytes())
        assert await _until(tripped.is_set, 3)
        session.end_turn()
        for pcm in (silence(2), speech_like(2000, seed=5), silence(2)):
            session.stream.feed(pcm.tobytes())
        assert await session.transcripts.get_async(3) == 'fresh request'
        assert await session.transcripts.get_async(3) == 'fresh request'
        assert len(called) == 2
    finally:
        session.stop()
        await session._worker



def test_cancel_at_transcript_boundary_never_starts_the_agent():
    session = ConverseSession(np)
    session.transcripts.put('request')
    session.transcripts.put(None)
    calls, sent = [], []

    async def send(frame):
        sent.append(frame)
        if isinstance(frame, dict) and frame.get('type') == 'transcript':
            assert session.cancel(frame['turn_id'])

    async def run(*args, **kwargs):
        calls.append(True)
        return 'must not execute', None

    async def scenario():
        await drive_converse_turns(session=session, synth=_EchoSynth(), cap=4000,
            loop=asyncio.get_running_loop(), send_json=send, send_bytes=send, run_turn=run, history=[],
            transcripts=True)

    asyncio.run(scenario())
    assert calls == []
    assert _types(sent) == ['transcript', 'interrupted', 'turn_done']
    assert "next_action" not in _turn_done_frame(sent)



@pytest.mark.asyncio
async def test_managed_input_reserves_transcript_before_driver_can_begin_thinking():
    session = ConverseSession(np, transcribe=lambda path: 'first request')
    session.enable_turn_control()
    session.start()
    try:
        for pcm in (calibration(), speech_like(2000, seed=8), silence(1.8)):
            session.stream.feed(pcm.tobytes())
        first = await session.transcripts.get_async(3)
        assert first == 'first request'
        assert session.capture_pending()
        for pcm in (speech_like(5000, seed=9), silence(1.8)):
            session.stream.feed(pcm.tobytes())
        # Continued speech supersedes a request whose driver has not started yet.
        assert await session.transcripts.get_async(3) == 'first request'
        stop = threading.Event()
        session.reserve_turn('superseded', stop, first)
        assert stop.is_set()
    finally:
        session.stop()
        await session._worker



def test_signoff_preserves_queued_conversation_output_in_arrival_order():
    session = _FakeConverseSession(['bye', cl.ConversationOutput('First.', 'first'),
                                  cl.ConversationOutput('Second.', 'second')])
    sent = _run_driver(session, [], ['Goodbye.'], quiet_interval=1, standby=lambda: True)
    ids = [frame['event_id'] for frame in sent if isinstance(frame, dict)
           and frame.get('type') == 'thinking' and 'event_id' in frame]
    assert ids == ['first', 'second']

@pytest.mark.parametrize("enabled", [True, False])
def test_voice_transcripts_are_opt_in(monkeypatch, enabled):
    from hermes_converse import speech

    monkeypatch.setattr(speech, "strip_markdown", lambda text: text)
    class Chunker:
        buf = ""
        def feed(self, text):
            return [text]
        def flush(self):
            return []
    monkeypatch.setattr(speech, "sentence_chunker", Chunker)
    monkeypatch.setattr("hermes_converse.loop.split_text_for_tts_stream", lambda text, cap: [text])
    sent = _run_driver(_FakeConverseSession(["Hello"]), [], ["Hello there."], transcripts=enabled)
    records = [frame for frame in sent if isinstance(frame, dict) and frame.get("type") == "transcript"]
    if enabled:
        assert [(record["role"], record["text"]) for record in records] == [("user", "Hello"), ("assistant", "Hello there.")]
        assert records[0]["turn_id"] == records[1]["turn_id"]
        assert sent.index(records[1]) < next(i for i, frame in enumerate(sent) if isinstance(frame, dict) and frame.get("type") == "turn_done")
    else:
        assert records == []
    assert "bytes" in _types(sent)
    from hermes_converse.loop import ConversationOutput
    outbound = _run_driver(_FakeConverseSession([ConversationOutput("Reminder.", "event-1")]), [], [], transcripts=enabled)
    outbound_text = [f for f in outbound if isinstance(f, dict) and f.get("type") == "transcript"]
    assert [(f["role"], f["text"]) for f in outbound_text] == ([("assistant", "Reminder.")] if enabled else [])


def _timing_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("converse timing:")]


def test_timing_line_identifies_the_turn_and_its_outcome(caplog):
    session = _FakeConverseSession(["hello"])
    session.session_id = "sess-1"
    with caplog.at_level("INFO", logger="hermes_converse.loop"):
        sent = _run_driver(session, [], ["Hi there."])
    (line,) = _timing_lines(caplog)
    assert f"turn={_turn_done_frame(sent)['turn_id']}" in line
    assert "session=sess-1" in line and "outcome=completed" in line
    assert "reply_chars=9" in line and "final_chars=9" in line and "spoken_chars=9" in line
    assert "next_action=listen" in line and "pcm_chunks=0" not in line
    assert "Hi there" not in caplog.text and "hello" not in caplog.text


@pytest.mark.parametrize("failure", ["cancel", "error"])
def test_timing_line_does_not_count_partial_output_as_a_final_reply(caplog, failure):
    session = ConverseSession(np)
    session.transcripts.put("hello")
    session.transcripts.put(None)
    sent, history = [], []

    async def send(frame):
        sent.append(frame)

    async def run(transcript, on_delta, *, interrupted):
        on_delta("Partial reply.")
        if failure == "cancel":
            session.cancel()
            await asyncio.Event().wait()
        raise RuntimeError("agent failed before returning a final reply")

    async def scenario():
        await drive_converse_turns(
            session=session, synth=_EchoSynth(), cap=4000,
            loop=asyncio.get_running_loop(), send_json=send, send_bytes=send,
            run_turn=run, history=history, tail_fallback=0)

    with caplog.at_level("INFO", logger="hermes_converse.loop"):
        asyncio.run(scenario())
    (line,) = _timing_lines(caplog)
    assert "reply_chars=14" in line and "final_chars=0" in line
    assert f"outcome={'barged' if failure == 'cancel' else 'error'}" in line
    assert history[-1] == {"role": "assistant", "content": "Partial reply."}
    assert _types(sent)[-2:] == ["interrupted" if failure == "cancel" else "error", "turn_done"]
    assert "next_action" not in _turn_done_frame(sent)


def test_timing_line_counts_a_final_only_reply_without_speaking_it(caplog):
    sent, history = [], []

    async def send(frame):
        sent.append(frame)

    async def run(transcript, on_delta, *, interrupted):
        return "Final reply.", None

    async def scenario():
        await drive_converse_turns(
            session=_FakeConverseSession(["hello"]), synth=_EchoSynth(), cap=4000,
            loop=asyncio.get_running_loop(), send_json=send, send_bytes=send,
            run_turn=run, history=history)

    with caplog.at_level("INFO", logger="hermes_converse.loop"):
        asyncio.run(scenario())
    (line,) = _timing_lines(caplog)
    assert "outcome=completed" in line and "final_chars=12" in line
    assert "deltas=0" in line and "reply_chars=0" in line
    assert "spoken_chars=0" in line and "pcm_chunks=0" in line
    assert _types(sent) == ["state", "thinking", "turn_done"]
    assert history[-1] == {"role": "assistant", "content": "Final reply."}


def test_timing_line_marks_a_timed_out_turn(caplog):
    sent: list = []

    async def _sj(o):
        sent.append(o)

    async def _sb(d):
        sent.append(("bytes", d))

    async def _hang(transcript, on_delta, *, interrupted):
        await asyncio.Event().wait()

    async def _main():
        await drive_converse_turns(
            session=_FakeConverseSession(["hello"]), synth=_EchoSynth(), cap=4000,
            loop=asyncio.get_running_loop(), send_json=_sj, send_bytes=_sb, run_turn=_hang,
            history=[], turn_timeout=0.3)

    with caplog.at_level("INFO", logger="hermes_converse.loop"):
        asyncio.run(_main())
    (line,) = _timing_lines(caplog)
    assert "outcome=timed_out" in line and "deltas=0" in line and "final_chars=0" in line


# ── reply audio pacing ──

class _PaceSession:
    def set_playing(self, value, *, tts_stop=None):
        pass


def _pace_driver(sent, *, turn_timeout=0.0, rate=8000):
    loop = asyncio.get_running_loop()

    async def send_bytes(data):
        sent.append((loop.time(), len(data)))

    async def send_json(obj):
        pass

    return types.SimpleNamespace(loop=loop, synth=types.SimpleNamespace(sample_rate=rate),
                                 session=_PaceSession(), send_json=send_json, send_bytes=send_bytes,
                                 turn_timeout=turn_timeout)


def _pace_turn():
    return types.SimpleNamespace(pcm_q=asyncio.Queue(), tts_stop=threading.Event(), meta={}, pcm_chunks=0,
                                 ns={}, timed_out=False, result={}, state=None)


def _chunk(seconds, rate=8000):
    return b"\0\0" * int(rate * seconds)


def test_reply_audio_is_sent_at_most_the_lead_ahead_of_playback(monkeypatch):
    monkeypatch.setattr(cl, "MAX_AUDIO_LEAD_SECONDS", 0.2)

    async def main():
        sent = []
        d, turn = _pace_driver(sent), _pace_turn()
        for _ in range(10):
            turn.pcm_q.put_nowait(_chunk(0.1))
        turn.pcm_q.put_nowait(None)
        start = d.loop.time()
        await cl._stream_reply(d, turn, asyncio.get_running_loop().create_future())
        return start, sent

    start, sent = asyncio.run(main())
    assert len(sent) == 10
    assert all(t - start < 0.05 for t, _ in sent[:3])  # time to first audio is not delayed
    for i, (t, _) in enumerate(sent):
        assert i * 0.1 - (t - start) <= 0.2 + 0.02  # queued audio never exceeds the lead
    assert sent[-1][0] - start >= 0.9 - 0.2 - 0.02


def test_interruption_during_pacing_stops_sending_promptly(monkeypatch):
    monkeypatch.setattr(cl, "MAX_AUDIO_LEAD_SECONDS", 0.2)

    async def main():
        sent = []
        d, turn = _pace_driver(sent), _pace_turn()
        for _ in range(50):
            turn.pcm_q.put_nowait(_chunk(0.1))
        turn.pcm_q.put_nowait(None)
        d.loop.call_later(0.3, turn.tts_stop.set)
        start = d.loop.time()
        await cl._stream_reply(d, turn, asyncio.get_running_loop().create_future())
        return d.loop.time() - start, sent

    elapsed, sent = asyncio.run(main())
    assert elapsed < 0.3 + 0.1
    assert len(sent) < 10


def test_slow_tts_is_not_held_back_after_the_client_runs_dry(monkeypatch):
    monkeypatch.setattr(cl, "MAX_AUDIO_LEAD_SECONDS", 0.2)

    async def main():
        sent, arrived = [], []
        d, turn = _pace_driver(sent), _pace_turn()

        async def produce():
            for _ in range(4):
                await asyncio.sleep(0.15)  # slower than the 0.1 s of audio per chunk
                arrived.append(d.loop.time())
                turn.pcm_q.put_nowait(_chunk(0.1))
            turn.pcm_q.put_nowait(None)

        producer = asyncio.ensure_future(produce())
        await cl._stream_reply(d, turn, asyncio.get_running_loop().create_future())
        await producer
        return arrived, sent

    arrived, sent = asyncio.run(main())
    assert all(t - a < 0.03 for (t, _), a in zip(sent, arrived))


def test_pacing_waits_do_not_count_toward_the_turn_timeout(monkeypatch):
    monkeypatch.setattr(cl, "MAX_AUDIO_LEAD_SECONDS", 0.1)

    async def main():
        sent = []
        d, turn = _pace_driver(sent, turn_timeout=0.3), _pace_turn()
        for _ in range(8):
            turn.pcm_q.put_nowait(_chunk(0.1))  # 0.8 s of audio: pacing takes ~0.7 s
        turn.pcm_q.put_nowait(None)
        await cl._stream_reply(d, turn, asyncio.get_running_loop().create_future())
        return turn, sent

    turn, sent = asyncio.run(main())
    assert not turn.timed_out and len(sent) == 8


@pytest.mark.asyncio
@pytest.mark.parametrize('question_playing', [True, False])
async def test_pending_approval_listens_normally_once_its_question_has_played(question_playing):
    """While an approval waits, the open turn is not playback: after the question's audio has
    played out, a short "yes" is detected as in ordinary listening (no echo gate, no barge-in
    threshold). While the question is still playing, echo rejection still applies."""
    session = ConverseSession(np)
    session.begin_turn('waiting', tts_stop=threading.Event())
    session.set_playing(True)
    session.answer_hook = lambda text: True
    session.playback_until = cl.time.monotonic() + (60 if question_playing else -1)
    seen = []
    feed = session._detector.feed
    def spy(block, playing, *, interrupting=False):
        seen.append((playing, interrupting))
        return feed(block, playing, interrupting=interrupting)
    session._detector.feed = spy
    session.start()
    try:
        session.stream.feed(silence(0.3).tobytes())
        await _until(lambda: seen, 3)
    finally:
        session.stop()
        await session._worker
    assert seen
    assert all(args == ((True, True) if question_playing else (False, False)) for args in seen)


def test_turn_state_follows_an_approval_wait(monkeypatch):
    """The server says when the turn waits on the person: `listening` once the question has played
    and no answer is taken, `thinking` again once it is, `speaking` when speech plays again."""
    monkeypatch.setattr(cl, "STATE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(cl, "THINKING_FRAME_INTERVAL", 60.0)

    async def main():
        sent = []
        waiting = {"now": False}

        async def send_json(obj):
            sent.append(obj)

        d = types.SimpleNamespace(session=types.SimpleNamespace(awaiting_answer=lambda: waiting["now"]),
                                  send_json=send_json, gone=lambda: False)
        turn = types.SimpleNamespace(tts_stop=threading.Event(), meta={"turn_id": "t"}, state="speaking")
        task = asyncio.create_task(cl._report_thinking(d, turn))
        await asyncio.sleep(0.05)
        waiting["now"] = True
        await asyncio.sleep(0.05)
        waiting["now"] = False
        await asyncio.sleep(0.05)
        turn.tts_stop.set()
        await task
        return [f["state"] for f in sent if f["type"] == "state"]

    assert asyncio.run(main()) == ["listening", "thinking"]


def test_answer_taken_ends_the_wait_before_it_is_settled():
    session = ConverseSession(np)
    session.answer_hook = lambda text: True
    session.playback_until = 0.0
    assert session.awaiting_answer()
    assert session._answers_approval("yes")
    assert not session.awaiting_answer()
    session.answer_hook = None
    assert not session.awaiting_answer()


def test_a_state_report_ends_the_tail_only_when_it_leaves_speaking_for_that_turn():
    session = ConverseSession(np)
    session.begin_playback_tail("turn-1", 5)
    session.playback_ended("turn-0")  # another turn: ignored
    assert session._tail_active
    session.playback_ended("turn-1")
    assert not session._tail_active and session._tail_result[1] == "reported"


def test_waiting_for_transcripts_and_the_playback_tail_holds_no_pool_thread():
    """Every connection waits on its transcripts (and its playback tail) for its whole life. Those
    waits must not take threads from the shared pool, or connection setup (``to_thread``) stalls
    once a few devices are connected."""
    from concurrent.futures import ThreadPoolExecutor

    async def main():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        sessions = [ConverseSession(np) for _ in range(4)]
        for session in sessions:
            session.begin_playback_tail("t", 30.0)
        gets = [asyncio.ensure_future(s.transcripts.get_async()) for s in sessions]
        tails = [asyncio.ensure_future(s.wait_for_playback_tail()) for s in sessions]
        await asyncio.sleep(0.05)
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "free"), 1) == "free"
        threads = [threading.Thread(target=s.transcripts.put, args=(f"heard {i}",)) for i, s in enumerate(sessions)]
        for t in threads:
            t.start()
        assert await asyncio.wait_for(asyncio.gather(*gets), 1) == [f"heard {i}" for i in range(4)]
        for session in sessions:
            threading.Thread(target=session.playback_ended, args=("t",)).start()
        await asyncio.wait_for(asyncio.gather(*tails), 1)
        assert not any(s._tail_active for s in sessions)

    asyncio.run(main())
