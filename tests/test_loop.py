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
    HISTORY_MAX_MESSAGES, MAX_ENDPOINT_HOLDS, ConverseSession, QuietTick, TailBarge, Utterance,
    drive_converse_turns, parse_start_config, voice_system_prompt)
from hermes_converse.synth import decode_audio_file_to_pcm16, resample_synth, resolve_synthesizer

from tests.support.converse_audio import calibration, silence, speech_like


# ── Smart Turn adaptive endpoint ──

class _ScriptedDetector:
    def __init__(self, probs):
        self._probs = list(probs)
        self.threshold = 0.5
        self.calls = 0

    def turn_complete_probability(self, audio_f32):
        p = self._probs[min(self.calls, len(self._probs) - 1)]
        self.calls += 1
        return float(p)


def _adaptive_session(detector, *, endpoint_blocks=2, max_blocks=60, transcribe=None):
    session = ConverseSession(np, transcribe=transcribe)  # endpoint_model off -> no model load
    session._turn_detector = detector
    session._endpoint_threshold = detector.threshold
    session._endpoint_blocks = endpoint_blocks
    session._max_blocks = max_blocks
    return session


def _feed_silence(session, blocks):
    silence_block = np.zeros(session._block, dtype=np.int16).tobytes()
    for _ in range(blocks):
        session.stream.feed(silence_block)


def test_adaptive_commits_immediately_when_turn_sounds_complete():
    det = _ScriptedDetector([0.95])
    session = _adaptive_session(det)
    _feed_silence(session, session._endpoint_blocks + 4)
    wav_path = session._capture_adaptive(silence_rms=float(vad.SILENCE_RMS_THRESHOLD))
    assert det.calls == 1
    with wave.open(wav_path, "rb") as wf:
        assert wf.getnframes() > 0
    vad.unlink_quietly(wav_path)


def test_adaptive_holds_then_commits_capturing_more_audio():
    det = _ScriptedDetector([0.1, 0.2, 0.9])
    session = _adaptive_session(det)
    _feed_silence(session, session._endpoint_blocks * 4 + 4)
    wav_path = session._capture_adaptive(silence_rms=float(vad.SILENCE_RMS_THRESHOLD))
    assert det.calls == 3
    vad.unlink_quietly(wav_path)


def test_adaptive_gives_up_after_max_holds():
    det = _ScriptedDetector([0.0])
    session = _adaptive_session(det, max_blocks=500)
    _feed_silence(session, session._endpoint_blocks * 6 + 4)
    wav_path = session._capture_adaptive(silence_rms=float(vad.SILENCE_RMS_THRESHOLD))
    assert det.calls == MAX_ENDPOINT_HOLDS
    vad.unlink_quietly(wav_path)


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
    assert session._capture_and_transcribe() == "hello there"
    import os
    assert not os.path.exists(seen["path"])


def test_transcribe_failure_is_nothing_heard():
    def _boom(path):
        raise RuntimeError("provider down")

    session = _adaptive_session(_ScriptedDetector([0.9]), transcribe=_boom)
    _feed_silence(session, session._endpoint_blocks + 4)
    assert session._capture_and_transcribe() == ""


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


# ── start frame + prompt ──

def test_parse_start_config_defaults_and_clamps():
    assert parse_start_config({}) == (16000, 24000, 0.0, "Sakura", None)
    assert parse_start_config({"input_rate": 100, "output_rate": "99000", "quiet_interval": "15",
                               "name": " Hal ", "profile": "work"}) == (8000, 48000, 15.0, "Hal", "work")
    assert parse_start_config({"name": "  "}, default_name="Nova")[3] == "Nova"
    assert parse_start_config({"quiet_interval": -3})[2] == 0.0


def test_voice_system_prompt_signoff_instruction():
    with_signoff = voice_system_prompt("Sakura", allow_signoff=True)
    assert "Over and out" in with_signoff and "stops listening" in with_signoff
    assert "Over and out" not in voice_system_prompt("Sakura", allow_signoff=False)
    assert "Over and out" not in voice_system_prompt("Sakura", allow_signoff=True, end_phrases=())
    assert "Your name is Sakura" in with_signoff and "Your name" not in voice_system_prompt(None)


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

    def take_interrupted(self):
        return False

    def set_playing(self, value, *, tts_stop=None):
        return None

    def begin_turn(self):
        self.turns_begun += 1

    def end_turn(self):
        self.turns_ended += 1

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
    return next(f for f in sent if isinstance(f, dict) and f.get("type") == "turn_done")


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


def test_drive_converse_turns_stop_word_ends_exchange():
    session = _FakeConverseSession(["Goodbye!"])
    history: list = []
    sent = _run_driver(session, history, ["SHOULD NOT RUN"], quiet_interval=1.0, stop_phrases=("goodbye",))
    assert _types(sent) == ["transcript", "stop_word"]
    assert sent[1]["text"] == "Goodbye!" and history == [] and session.turns_begun == 0


def test_drive_converse_turns_stop_word_ignored_in_continuous_mode():
    history: list = []
    sent = _run_driver(_FakeConverseSession(["goodbye"]), history, ["Bye!"], quiet_interval=0.0,
                       stop_phrases=("goodbye",))
    assert _types(sent) == ["transcript", "thinking", "speaking", "bytes", "turn_done"]


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
    assert len(turn_dones) == 1 and turn_dones[0].get("expects_more") is False


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
    assert sent == [{"type": "interrupted", "turn_id": "turn-x"}] and session.turns_begun == 0


def test_drive_converse_turns_signoff_sets_expects_more_false():
    sent = _run_driver(_FakeConverseSession(["are we done?"]), [], ["All set. Over and out."], quiet_interval=1.0)
    assert _turn_done_frame(sent).get("expects_more") is False


def test_drive_converse_turns_signoff_is_not_spoken_but_still_flags():
    sent = _run_driver(_FakeConverseSession(["bye"]), [], ["See you later. Over and out."], quiet_interval=1.0)
    spoken = _spoken_bytes(sent).lower()
    assert b"see you later" in spoken and b"over and out" not in spoken
    assert _turn_done_frame(sent).get("expects_more") is False


def test_drive_converse_turns_signoff_spoken_in_continuous_mode():
    sent = _run_driver(_FakeConverseSession(["bye"]), [], ["See you later. Over and out."], quiet_interval=0.0)
    assert b"over and out" in _spoken_bytes(sent).lower()


def test_drive_converse_turns_question_sets_expects_more_true():
    sent = _run_driver(_FakeConverseSession(["set a timer"]), [], ["Sure — for how long?"], quiet_interval=1.0)
    assert _turn_done_frame(sent).get("expects_more") is True


def test_drive_converse_turns_plain_reply_has_no_expects_more():
    sent = _run_driver(_FakeConverseSession(["what's the weather?"]), [], ["It's sunny and 72."], quiet_interval=1.0)
    assert "expects_more" not in _turn_done_frame(sent)


def test_drive_converse_turns_continuous_mode_never_sets_expects_more():
    for reply in ("All set. Over and out.", "For how long?"):
        sent = _run_driver(_FakeConverseSession(["hi"]), [], [reply], quiet_interval=0.0)
        assert "expects_more" not in _turn_done_frame(sent)


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
