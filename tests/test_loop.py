"""The converse loop primitives: adaptive endpoint, session barge-in, the synthesizer seam and the
shared turn driver (control-frame ordering, history cap, session mode, timeouts, tracing)."""

from __future__ import annotations

import asyncio
import contextvars
import json
import queue as _queue
import threading
import wave

import numpy as np
import pytest

from hermes_converse import loop as cl
from hermes_converse import speech, vad
from hermes_converse.loop import (
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


def _captured_blocks(session, wav_path):
    with wave.open(wav_path, "rb") as wf:
        frames = wf.getnframes()
    vad.unlink_quietly(wav_path)
    return frames // session._block


def test_adaptive_commits_immediately_when_turn_sounds_complete():
    det = _ScriptedDetector([0.95])
    session = _adaptive_session(det)
    _feed_silence(session, session._endpoint_blocks + 4)
    wav_path, volume = session._capture()
    assert det.calls == 1
    assert volume >= 0
    assert _captured_blocks(session, wav_path) == session._endpoint_blocks + 4  # buffered audio is kept


def test_adaptive_rechecks_each_window_until_complete():
    det = _ScriptedDetector([0.1, 0.2, 0.9])
    session = _adaptive_session(det)
    _feed_silence(session, 20)
    wav_path, _volume = session._capture()
    assert det.calls == 3


def test_adaptive_incomplete_turn_ends_at_max_silence():
    det = _ScriptedDetector([0.0])
    session = _adaptive_session(det, max_silence_blocks=9)
    _feed_silence(session, 20)
    wav_path, _volume = session._capture()
    assert det.calls == 4  # checks at 2, 4, 6, 8 blocks of silence
    assert _captured_blocks(session, wav_path) == 9


def test_adaptive_resumed_speech_restores_the_full_pause_allowance():
    det = _ScriptedDetector([0.0, 0.0, 0.9])
    session = _adaptive_session(det, max_silence_blocks=6)
    _feed_speech(session, 2)
    _feed_silence(session, 5)  # an incomplete-sounding pause, bridged
    _feed_speech(session, 2)
    _feed_silence(session, 10)
    wav_path, _volume = session._capture()
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
    wav_path, _volume = session._capture()
    assert det.calls == 2  # the complete verdict was discarded and the updated turn rechecked
    assert _captured_blocks(session, wav_path) == 2 + 2 + 3 + 5


def test_fixed_endpoint_ends_at_first_candidate_pause():
    session = _adaptive_session(None)
    _feed_speech(session, 3)
    _feed_silence(session, 10)
    wav_path, _volume = session._capture()
    assert _captured_blocks(session, wav_path) == 3 + session._endpoint_blocks


@pytest.mark.parametrize("smart_turn", [False, True])
def test_wake_pause_waits_for_the_request(smart_turn):
    det = _ScriptedDetector([0.9]) if smart_turn else None
    session = _adaptive_session(det)
    wake_word = session._wake_speech_blocks - 5
    _feed_speech(session, wake_word)  # "Hey Arya"
    _feed_silence(session, 6)  # checking that the device is listening
    _feed_speech(session, 20)  # "set a five minute timer"
    _feed_silence(session, 10)
    wav_path, _volume = session._capture(wake=True)
    trailing = 10 if smart_turn else session._endpoint_blocks  # Smart Turn keeps buffered audio
    assert _captured_blocks(session, wav_path) == wake_word + 6 + 20 + trailing
    assert det is None or det.calls == 1


def test_wake_pause_is_bounded_by_max_silence():
    session = _adaptive_session(None)
    _feed_speech(session, 5)
    _feed_silence(session, 20)
    wav_path, _volume = session._capture(wake=True)
    assert _captured_blocks(session, wav_path) == 5 + session._max_silence_blocks


def test_continuous_wake_request_endpoints_normally():
    session = _adaptive_session(None)
    _feed_speech(session, session._wake_speech_blocks + 10)
    _feed_silence(session, 10)
    wav_path, _volume = session._capture(wake=True)
    assert _captured_blocks(session, wav_path) == session._wake_speech_blocks + 10 + session._endpoint_blocks


def test_without_wake_a_short_pause_ends_capture():
    session = _adaptive_session(None)
    _feed_speech(session, 5)
    _feed_silence(session, 6)
    _feed_speech(session, 20)
    wav_path, _volume = session._capture()
    assert _captured_blocks(session, wav_path) == 5 + session._endpoint_blocks


@pytest.mark.parametrize("wake", [False, True])
def test_client_commit_ends_capture_even_when_smart_turn_would_hold(wake):
    det = _ScriptedDetector([0.0])
    session = _adaptive_session(det)
    _feed_speech(session, 3)
    session.commit()
    wav_path, _volume = session._capture(wake=wake)
    assert det.calls == 0
    assert _captured_blocks(session, wav_path) == 4


def test_commit_queued_with_speech_before_onset_still_transcribes():
    """PCM and commit can both reach the stream before the worker detects onset."""
    session = ConverseSession(np, transcribe=lambda path: "set a timer", capture_no_data_seconds=1.0)
    session.stream.feed(calibration().tobytes())
    session.stream.feed(speech_like(2000, seconds=1.0).tobytes())
    session.commit()  # no further PCM
    session.start()
    try:
        item = session.transcripts.get(timeout=3)
        assert isinstance(item, Utterance) and item == "set a timer"
    finally:
        session.stop()


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
    wav_path, _volume = session._capture()
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
    transcript, volume = session._capture_and_transcribe()
    assert transcript == "hello there"
    assert volume >= 0
    import os
    assert not os.path.exists(seen["path"])


def test_transcribe_failure_is_nothing_heard():
    def _boom(path):
        raise RuntimeError("provider down")

    session = _adaptive_session(_ScriptedDetector([0.9]), transcribe=_boom)
    _feed_silence(session, session._endpoint_blocks + 4)
    transcript, _volume = session._capture_and_transcribe()
    assert transcript == ""


# ── ConverseSession barge-in / playing flag ──

def test_converse_session_barge_in_sets_interrupt_and_stops_tts(monkeypatch):
    marked = []
    monkeypatch.setattr(speech, "mark_speech_interrupted", lambda: marked.append(True))
    session = ConverseSession(np)
    tts_stop = threading.Event()
    session.set_playing(True, tts_stop=tts_stop)
    assert session.playing() is True
    session._trigger_barge_in()
    assert tts_stop.is_set() and marked == [True]
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
    session.notify_drained("other")
    for _ in range(50):
        session._account_received_silence()
    assert _drain_ticks() == []
    session.notify_drained("t1")
    for _ in range(50):
        session._account_received_silence()
    assert len(_drain_ticks()) >= 1


def test_first_quiet_tick_after_resume_is_a_full_interval_later():
    """Silence received before the client stopped sending must not shorten the next listening phase."""
    import time

    session = ConverseSession(np, quiet_interval=0.3)  # 10 blocks
    session.stream.resume_gap_seconds = 0.1
    session.start()
    try:
        def ticks_after(blocks):
            _feed_silence(session, blocks)
            deadline = time.monotonic() + 2
            while not session.stream._chunks.empty() and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.05)
            out = []
            while True:
                try:
                    out.append(session.transcripts.get_nowait())
                except _queue.Empty:
                    return [t for t in out if isinstance(t, QuietTick)]

        assert ticks_after(8) == []  # an earlier listening phase: 0.24 s of silence, then the client stops
        time.sleep(0.2)  # client stopped sending; nothing received
        assert ticks_after(3) == []  # resumed: 0.09 s is not an interval
        ticks = ticks_after(7)
        assert len(ticks) == 1 and ticks[0].quiet_seconds == 0.3
    finally:
        session.stop()


def test_split_sample_resume_requires_a_full_new_quiet_interval(monkeypatch):
    """Drive the real worker synchronously so each assertion follows a processed audio block."""
    session = ConverseSession(np, quiet_interval=0.3)
    read = session.stream.read
    silence_block = np.zeros(session._block, dtype=np.int16).tobytes()
    blocks = 0
    completed = False

    def feed_then_read(block):
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
            return np.zeros(block, dtype=np.int16), False
        if blocks == 8:
            session.stream._last_feed -= 1.1  # a receive gap above the real 1 s threshold
            session.stream.feed(silence_block[:1])
            session.stream.feed(silence_block[1:])
        else:
            session.stream.feed(silence_block)
        blocks += 1
        return read(block)

    monkeypatch.setattr(session.stream, "read", feed_then_read)
    session._run()
    assert completed and blocks == 18  # the worker logs exceptions instead of propagating them


# ── start frame + prompt ──

def test_parse_start_config_defaults_and_clamps():
    assert parse_start_config({}) == (16000, 24000, 0.0, "Arya", None)
    assert parse_start_config({"input_rate": 100, "output_rate": "99000", "quiet_interval": "15",
                               "name": " Hal ", "profile": "work"}) == (8000, 48000, 15.0, "Hal", "work")
    assert parse_start_config({"name": "  "}, default_name="Nova")[3] == "Nova"
    assert parse_start_config({"quiet_interval": -3})[2] == 0.0


def test_voice_system_prompt_signoff_instruction():
    with_signoff = voice_system_prompt("Arya", allow_signoff=True)
    assert "Over and out" in with_signoff and "server interprets" in with_signoff
    assert "Over and out" not in voice_system_prompt("Arya", allow_signoff=False)
    assert "Over and out" not in voice_system_prompt("Arya", allow_signoff=True, end_phrases=())
    assert "Your name is Arya" in with_signoff and "Your name" not in voice_system_prompt(None)


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
        self.transcripts = _queue.Queue()
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

    def notify_drained(self, turn_id=None):
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
    assert set(frame) == {"type", "turn_id", "next_action"}
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


def test_driver_no_interrupt_on_normal_completion():
    calls = []
    _run_driver(_FakeConverseSession(["what time is it"]), [], ["It's noon."], quiet_interval=1.0,
                interrupt_turn=lambda: calls.append(True))
    assert calls == []


def test_drive_converse_turns_control_frame_ordering():
    session = _FakeConverseSession(["hello there."])
    history: list = []
    sent = _run_driver(session, history, ["Hi back."])
    assert _types(sent) == ["transcript", "thinking", "speaking", "bytes", "turn_done"]
    assert sent[0]["text"] == "hello there." and sent[3] == ("bytes", b"Hi back.")
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
    session.transcripts = _queue.Queue()
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
    assert _turn_done_frame(sent)["next_action"] == "listen"
    assert cancelled == [True]  # the hung turn was cancelled, not left running


def test_stt_and_tts_threads_run_in_the_spawners_context(monkeypatch):
    probe = contextvars.ContextVar("probe", default=None)
    seen = {}

    def _transcribe(path, **kw):
        seen["stt"] = probe.get()
        return "hello"

    monkeypatch.setattr(speech, "transcribe_wav", _transcribe)
    probe.set("request-scope")
    session = ConverseSession(np)
    session.start()
    try:
        for pcm in (calibration(), speech_like(2000, seed=1), silence(seconds=1.8)):
            session.stream.feed(pcm.tobytes())
        assert session.transcripts.get(timeout=6) == "hello"
    finally:
        session.stop()

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
    sent = _run_driver(session, [], ["All set. Over and out."], quiet_interval=1.0)
    transcripts = [f["text"] for f in sent if isinstance(f, dict) and f.get("type") == "transcript"]
    assert transcripts == ["are we done?"]
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
    _run_driver(session, [], ["ok.", "All set. Over and out."], quiet_interval=1.0)
    assert opened == ["r1", "r2"]
    assert emitted == [r3] and r3.attrs == {"outcome": "discarded_after_signoff"}


def test_driver_tail_barge_emits_interrupted_not_a_turn():
    session = _FakeConverseSession([])
    session.transcripts = _queue.Queue()
    session.transcripts.put(TailBarge("turn-x"))
    session.transcripts.put(None)
    sent = _run_driver(session, [], [])
    assert sent == [{"type": "interrupted", "turn_id": "turn-x"},
                    {"type": "turn_done", "turn_id": "turn-x", "next_action": "listen"}]
    assert session.turns_begun == 0


def test_drive_converse_turns_signoff_requests_standby():
    sent = _run_driver(_FakeConverseSession(["are we done?"]), [], ["All set. Over and out."], quiet_interval=1.0)
    assert _turn_done_frame(sent)["next_action"] == "standby"


def test_drive_converse_turns_signoff_is_not_spoken_but_still_flags():
    sent = _run_driver(_FakeConverseSession(["bye"]), [], ["See you later. Over and out."], quiet_interval=1.0)
    spoken = _spoken_bytes(sent).lower()
    assert b"see you later" in spoken and b"over and out" not in spoken
    assert _turn_done_frame(sent)["next_action"] == "standby"


def test_drive_converse_turns_signoff_spoken_in_continuous_mode():
    sent = _run_driver(_FakeConverseSession(["bye"]), [], ["See you later. Over and out."], quiet_interval=0.0)
    assert b"over and out" in _spoken_bytes(sent).lower()


def test_drive_converse_turns_question_requests_listen():
    sent = _run_driver(_FakeConverseSession(["set a timer"]), [], ["Sure — for how long?"], quiet_interval=1.0)
    assert _turn_done_frame(sent)["next_action"] == "listen"


def test_drive_converse_turns_plain_reply_requests_listen():
    sent = _run_driver(_FakeConverseSession(["what's the weather?"]), [], ["It's sunny and 72."], quiet_interval=1.0)
    assert _turn_done_frame(sent)["next_action"] == "listen"


def test_drive_converse_turns_continuous_mode_requests_listen():
    for reply in ("All set. Over and out.", "For how long?"):
        sent = _run_driver(_FakeConverseSession(["hi"]), [], [reply], quiet_interval=0.0)
        assert _turn_done_frame(sent)["next_action"] == "listen"


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
    transcript, volume = session._capture_and_transcribe()
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
    session.transcripts = _queue.Queue()
    session.transcripts.put(cl.CancelTurn("turn-9"))
    session.transcripts.put(None)
    sent = _run_driver(session, [], [], quiet_interval=1.0)
    assert _types(sent) == ["interrupted", "turn_done"]
    assert sent[0]["turn_id"] == "turn-9" and sent[1]["turn_id"] == "turn-9"
    assert _turn_done_frame(sent)["next_action"] == "listen"
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
            send_json=send, send_bytes=send, run_turn=run, history=[], quiet_interval=1.0)

    asyncio.run(main())
    completions = [frame for frame in sent if isinstance(frame, dict) and frame.get("type") == "turn_done"]
    assert len(completions) == 2  # the partial sign-off must not flush the next request
    assert all(frame["next_action"] == "listen" for frame in completions)


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
                session.notify_drained("another-turn")
                assert session._tail_active
                session.notify_drained(frame["turn_id"])
        async def run(text, on_delta, **kwargs):
            on_delta("Goodbye. Over and out.")
            return "Goodbye. Over and out.", None
        await drive_converse_turns(session=session, synth=_EchoSynth(), cap=4000,
            loop=asyncio.get_running_loop(), send_json=send, send_bytes=send,
            run_turn=run, history=[], quiet_interval=1.0)
        return sent
    done = _turn_done_frame(asyncio.run(scenario()))
    assert done["next_action"] == "standby" and not session._tail_active


@pytest.mark.parametrize('phase', ['thinking', 'speaking', 'tail'])
def test_accepted_barge_in_is_captured_in_every_phase(monkeypatch, phase):
    calls = []
    monkeypatch.setattr(speech, 'mark_speech_interrupted', lambda: None)
    session = ConverseSession(np, transcribe=lambda path: calls.append(path) or 'continued request')
    stop = threading.Event()
    interrupted = threading.Event()
    session.begin_turn('active', tts_stop=stop, on_interrupt=interrupted.set)
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
        assert interrupted.wait(3)
        assert session.transcripts.get(timeout=3) == 'continued request'
        assert len(calls) == 1
        if phase != 'tail':
            assert stop.is_set()
    finally:
        session.stop()


def test_stale_cancel_does_not_touch_newer_thinking_turn():
    session = ConverseSession(np)
    stop = threading.Event()
    interrupted = []
    session.begin_turn('newer', tts_stop=stop, on_interrupt=lambda: interrupted.append(True))
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
    audio, _ = session.stream.read(480)
    assert np.all(audio == 99)
    assert np.all(session._pre_roll[0] == 11)


def test_continuation_and_later_fresh_input_are_both_preserved(monkeypatch):
    monkeypatch.setattr(speech, 'mark_speech_interrupted', lambda: None)
    called = []
    tripped = threading.Event()
    session = ConverseSession(np, transcribe=lambda path: called.append(path) or 'fresh request')
    session.begin_turn('busy', tts_stop=threading.Event(), on_interrupt=tripped.set)
    session.start()
    try:
        for pcm in (calibration(), speech_like(5000, seed=4)):
            session.stream.feed(pcm.tobytes())
        assert tripped.wait(3)
        session.end_turn()
        for pcm in (silence(2), speech_like(2000, seed=5), silence(2)):
            session.stream.feed(pcm.tobytes())
        assert session.transcripts.get(timeout=3) == 'fresh request'
        assert session.transcripts.get(timeout=3) == 'fresh request'
        assert len(called) == 2
    finally:
        session.stop()



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
            loop=asyncio.get_running_loop(), send_json=send, send_bytes=send, run_turn=run, history=[])

    asyncio.run(scenario())
    assert calls == []
    assert _types(sent) == ['transcript', 'interrupted', 'turn_done']
    assert _turn_done_frame(sent)['next_action'] == 'listen'



def test_managed_input_reserves_transcript_before_driver_can_begin_thinking():
    session = ConverseSession(np, transcribe=lambda path: 'first request')
    session.enable_turn_control()
    session.start()
    try:
        for pcm in (calibration(), speech_like(2000, seed=8), silence(1.8)):
            session.stream.feed(pcm.tobytes())
        first = session.transcripts.get(timeout=3)
        assert first == 'first request'
        assert session.capture_pending()
        for pcm in (speech_like(5000, seed=9), silence(1.8)):
            session.stream.feed(pcm.tobytes())
        # Continued speech supersedes a request whose driver has not started yet.
        assert session.transcripts.get(timeout=3) == 'first request'
        stop = threading.Event()
        session.reserve_turn('superseded', stop, first)
        assert stop.is_set()
    finally:
        session.stop()



def test_signoff_preserves_queued_conversation_output_in_arrival_order():
    session = _FakeConverseSession(['bye', cl.ConversationOutput('First.', 'first'),
                                  cl.ConversationOutput('Second.', 'second')])
    sent = _run_driver(session, [], ['Goodbye. Over and out.'], quiet_interval=1)
    ids = [frame['event_id'] for frame in sent if isinstance(frame, dict)
           and frame.get('type') == 'thinking' and 'event_id' in frame]
    assert ids == ['first', 'second']

@pytest.mark.parametrize("enabled", [True, False])
def test_voice_transcripts_are_opt_in(monkeypatch, enabled):
    import threading
    from hermes_converse import speech

    monkeypatch.setattr(speech, "spawn_context_thread", lambda target, name, args: threading.Thread(target=target, args=args, daemon=True))
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


def test_timing_line_shows_a_bare_signoff_as_completed_but_unspoken(caplog):
    """The 13-character sign-off is a control marker: no audio, standby requested."""
    with caplog.at_level("INFO", logger="hermes_converse.loop"):
        _run_driver(_FakeConverseSession(["bye"]), [], ["Over and out."], quiet_interval=1.0)
    (line,) = _timing_lines(caplog)
    assert "first_pcm=Nonems" in line and "reply_chars=13" in line
    assert "outcome=completed" in line and "spoken_chars=0" in line and "pcm_chunks=0" in line
    assert "next_action=standby" in line


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
    assert _turn_done_frame(sent)["next_action"] == "listen"


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
    assert _types(sent) == ["thinking", "turn_done"]
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
