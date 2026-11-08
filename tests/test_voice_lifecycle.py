"""Input ownership and cancellation across the real microphone worker/turn driver."""
import asyncio
import threading
import wave

import numpy as np
import pytest

from hermes_converse import speech
from hermes_converse.loop import ConverseSession, Utterance, drive_converse_turns
from tests.support.converse_audio import calibration, silence, speech_like
from tests.test_loop import _EchoSynth


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setattr(speech, 'spawn_context_thread',
                        lambda target, **kw: threading.Thread(target=target, args=kw.get('args', ()), daemon=True))
    monkeypatch.setattr(speech, 'mark_speech_interrupted', lambda: None)
    sessions = []

    def create(**kwargs):
        session = ConverseSession(np, vad_model='energy', **kwargs)
        sessions.append(session)
        return session

    yield create
    for session in sessions:
        session.stop()
        if session._worker is not None:
            session._worker.join(timeout=3)
            assert not session._worker.is_alive()


def deliver(session, *, trailing=True):
    for pcm in (calibration(), speech_like(5000, seed=25)):
        session.stream.feed(pcm.tobytes())
    if trailing:
        session.stream.feed(silence(2).tobytes())


@pytest.mark.parametrize('cancel', [False, True], ids=['old-completion', 'explicit-cancel'])
def test_stt_capture_survives_old_completion_but_not_explicit_cancel(worker, cancel):
    entered, release = threading.Event(), threading.Event()
    captured = []

    def transcribe(path):
        with wave.open(path, 'rb') as wav:
            captured.append(np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2'))
        entered.set()
        release.wait(3)
        return 'and make it warm white'

    session = worker(transcribe=transcribe)
    session.enable_turn_control()
    session.begin_turn('old', tts_stop=threading.Event())
    session.start()
    try:
        deliver(session)
        assert entered.wait(3)
        if cancel:
            assert session.cancel('old')
        # Even a completed reply with queued speaker audio cannot arm a tail over this capture.
        assert session.finish_turn('old', 20)
        assert not session._tail_active
        session.notify_drained('old')
        release.set()
        result = session.transcripts.get(timeout=3)
        if cancel:
            assert not isinstance(result, str)  # InputReady, not a cancelled transcript
        else:
            assert result == 'and make it warm white'
        # The beginning survives the detector's sustained-onset delay.
        assert speech_like(5000, seed=25).tobytes() in captured[0].tobytes()
    finally:
        release.set()


def test_cancel_during_capture_endpoints_and_skips_stt(worker):
    onset = threading.Event()
    calls = []
    session = worker(transcribe=lambda path: calls.append(path) or 'cancelled input')
    session.enable_turn_control()
    session.begin_turn('old', tts_stop=threading.Event())
    session.on_trip = lambda phase: onset.set()
    session.start()
    deliver(session, trailing=False)
    assert onset.wait(3)
    session.cancel()  # commit injects silence so an unfinished capture can settle
    session.finish_turn('old', 0)
    session.stream.feed(silence(2).tobytes())
    item = session.transcripts.get(timeout=3)
    assert not isinstance(item, str) and calls == []


@pytest.mark.asyncio
async def test_multiple_unstarted_corrections_retain_original_context_once(worker):
    texts = iter(['turn on the light', 'and make it warm white', 'at half brightness'])
    session = worker(transcribe=lambda path: next(texts))
    session.enable_turn_control()
    session.start()
    items = []
    for _ in range(3):
        deliver(session)
        items.append(await asyncio.to_thread(session.transcripts.get, True, 3))
    assert all(isinstance(item, Utterance) for item in items)
    assert [item.superseded for item in items] == [True, True, False]
    for item in items:
        session.transcripts.put(item)
    session.transcripts.put(None)
    inputs, frames, history = [], [], []

    async def run(text, on_delta, **kwargs):
        inputs.append(text)
        on_delta('Done.')
        return 'Done.', None

    async def send(frame):
        frames.append(frame)
        if isinstance(frame, dict) and frame.get('type') == 'turn_done':
            session.notify_drained(frame['turn_id'])

    await drive_converse_turns(session=session, synth=_EchoSynth(), cap=4000,
        loop=asyncio.get_running_loop(), send_json=send, send_bytes=send,
        run_turn=run, history=history)
    assert inputs == ['turn on the light\nand make it warm white\nat half brightness']
    assert history == [{'role': 'user', 'content': inputs[0]},
                       {'role': 'assistant', 'content': 'Done.'}]
    assert len([f for f in frames if isinstance(f, dict) and f['type'] == 'thinking']) == 1
    assert not any(isinstance(f, bytes) for f in frames[:6])  # superseded turns send no PCM
