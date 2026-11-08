"""Wake audio must not become a request; exercise the real capture and driver."""
import asyncio
from contextlib import contextmanager

import pytest

from hermes_converse import loop as cl, speech
from hermes_converse.phrases import is_wake_only
from tests.test_arbitrate_integration import _speech_seams, _patch, _audio_turn
from tests.test_route import Harness, API_KEY, _speech_then_silence_pcm, _start_msg


@pytest.mark.parametrize('transcript', ['Hey Arya', 'Hey, Aria.', 'HEY ARYA!', '"Hey... Aria?"'])
def test_wake_only_ignores_punctuation_and_name_spelling(transcript):
    assert is_wake_only(transcript, ('Hey Arya',))


@pytest.mark.parametrize('transcript', [
    'Hey Arya, set a five minute timer', 'Hey Aria stop', 'Set a five minute timer',
    'Play an aria', 'Hey area', '', 'Hey',
])
def test_real_request_or_unrelated_text_is_not_wake_only(transcript):
    assert not is_wake_only(transcript, ('Hey Arya',))


class _CompleteTurn:
    def turn_complete_probability(self, audio):
        return 1.0


async def _connect(client, **config):
    ws = await client.ws_connect('/v1/audio/converse')
    await ws.send_str(_start_msg(key=API_KEY, name='Arya', **config))
    assert (await ws.receive_json(timeout=2))['type'] == 'ready'
    return ws


async def _wake(ws, claim='detection-1'):
    await ws.send_json({'type': 'wake', 'claim_id': claim, 'wake_word': 'Hey Arya'})
    assert (await ws.receive_json(timeout=1))['type'] == 'wake_grant'


@pytest.mark.parametrize('endpoint', ['fixed', 'smart', 'fallback'])
@pytest.mark.parametrize('wake_text', ['Hey, Aria.', 'Hey Arya', 'Aria'])
@pytest.mark.asyncio
async def test_wake_pause_request_produces_only_one_agent_turn(monkeypatch, endpoint, wake_text):
    from hermes_converse import turn_detector
    fake = _patch(monkeypatch)
    transcripts = iter([wake_text, 'Set a five minute timer.', 'Another request.'])
    monkeypatch.setattr(speech, 'transcribe_wav', lambda *a, **kw: next(transcripts, ''))
    monkeypatch.setattr(turn_detector, 'load_turn_detector',
                        lambda threshold: _CompleteTurn() if endpoint == 'smart' else None)
    suppressed = asyncio.Event()
    recorded = []
    class Recorder:
        def __init__(self, **attrs):
            self.attrs = attrs
        def set(self, **attrs):
            self.attrs.update(attrs)
        @contextmanager
        def phase(self, *args, **kwargs):
            yield self
    monkeypatch.setattr(cl, 'PhaseRecorder', Recorder)
    def on_suppressed(recorder):
        recorded.append(recorder)
        suppressed.set()
    monkeypatch.setattr(cl, 'emit_turn_trace', on_suppressed)
    settings = {'endpoint_model': '' if endpoint == 'fixed' else 'smart-turn-v3'}
    async with Harness(fake, settings=settings) as client:
        ws = await _connect(client)
        await _wake(ws)
        for chunk in _speech_then_silence_pcm():
            await ws.send_bytes(chunk)
        await asyncio.wait_for(suppressed.wait(), timeout=2)
        assert fake.runs == {}  # wake alone did not reach the agent
        assert recorded[0].attrs['outcome'] == 'wake_only'
        assert recorded[0].attrs['wake.suppressed'] is True
        assert recorded[0].attrs['endpoint.mode'] == ('smart_turn' if endpoint == 'smart' else 'fixed')
        # No transcript, thinking, reply PCM, or turn_done for the wake-only capture.
        with pytest.raises(asyncio.TimeoutError):
            await ws.receive(timeout=0.05)
        await _audio_turn(ws)
        assert len(fake.runs) == 1
        assert next(iter(fake.runs.values()))['body']['input'] == 'Set a five minute timer.'
        # Follow-ups have no wake filter, even when their text is the assistant's name.
        monkeypatch.setattr(speech, 'transcribe_wav', lambda *a, **kw: 'Aria')
        await _audio_turn(ws)
        assert len(fake.runs) == 2
        await ws.close()


@pytest.mark.parametrize('endpoint', ['fixed', 'smart', 'fallback'])
@pytest.mark.asyncio
async def test_continuous_wake_and_request_keeps_every_word(monkeypatch, endpoint):
    from hermes_converse import turn_detector
    fake = _patch(monkeypatch)
    text = 'Hey Aria, set a five minute timer.'
    monkeypatch.setattr(speech, 'transcribe_wav', lambda *a, **kw: text)
    monkeypatch.setattr(turn_detector, 'load_turn_detector',
                        lambda threshold: _CompleteTurn() if endpoint == 'smart' else None)
    async with Harness(fake, settings={'endpoint_model': '' if endpoint == 'fixed' else 'smart-turn-v3'}) as client:
        ws = await _connect(client)
        await _wake(ws)
        await _audio_turn(ws)
        assert next(iter(fake.runs.values()))['body']['input'] == text
        await ws.close()


@pytest.mark.asyncio
async def test_invalid_wake_does_not_arm_filter(monkeypatch):
    fake = _patch(monkeypatch)
    monkeypatch.setattr(speech, 'transcribe_wav', lambda *a, **kw: 'Hey Aria')
    async with Harness(fake) as client:
        ws = await _connect(client)
        await ws.send_json({'type': 'wake', 'wake_word': 'Hey Arya'})
        assert (await ws.receive_json(timeout=1))['error'] == 'bad_wake'
        await _audio_turn(ws)
        assert len(fake.runs) == 1
        await ws.close()


@pytest.mark.asyncio
async def test_stop_phrase_has_priority_over_wake_only_guard(monkeypatch):
    fake = _patch(monkeypatch)
    monkeypatch.setattr(speech, 'transcribe_wav', lambda *a, **kw: 'Stop!')
    async with Harness(fake) as client:
        ws = await _connect(client, quiet_interval=3)
        await ws.send_json({'type': 'wake', 'claim_id': 'stop-wake', 'wake_word': 'stop'})
        assert (await ws.receive_json(timeout=1))['type'] == 'wake_grant'
        for chunk in _speech_then_silence_pcm():
            await ws.send_bytes(chunk)
        frames = []
        while True:
            frame = await ws.receive_json(timeout=2)
            frames.append(frame)
            if frame['type'] == 'stop_word':
                break
        assert [f['type'] for f in frames] == ['transcript', 'stop_word']
        assert fake.runs == {}
        await ws.close()


@pytest.mark.asyncio
async def test_new_wake_during_capture_applies_to_next_capture(monkeypatch):
    fake = _patch(monkeypatch)
    sessions = []
    original_note_wake = cl.ConverseSession.note_wake
    def remember_session(self, *args, **kw):
        sessions.append(self)
        original_note_wake(self, *args, **kw)
    monkeypatch.setattr(cl.ConverseSession, 'note_wake', remember_session)
    calls = 0
    def transcribe(*args, **kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            # A new detection arrives after capture onset but before the old STT finishes.
            sessions[0].note_wake('Computer', name='Computer')
            return 'Hey Aria'
        return 'Computer' if calls == 2 else 'Set a timer'
    monkeypatch.setattr(speech, 'transcribe_wav', transcribe)
    suppressed = asyncio.Queue()
    monkeypatch.setattr(cl, 'emit_turn_trace', lambda recorder: suppressed.put_nowait(recorder))
    async with Harness(fake) as client:
        ws = await _connect(client)
        await _wake(ws)
        for _ in range(2):
            for chunk in _speech_then_silence_pcm():
                await ws.send_bytes(chunk)
            await asyncio.wait_for(suppressed.get(), timeout=2)
            assert fake.runs == {}
        await _audio_turn(ws)
        assert len(fake.runs) == 1
        assert next(iter(fake.runs.values()))['body']['input'] == 'Set a timer'
        await ws.close()


@pytest.mark.asyncio
async def test_wake_alone_keeps_normal_quiet_notifications(monkeypatch):
    fake = _patch(monkeypatch)
    monkeypatch.setattr(speech, 'transcribe_wav', lambda *a, **kw: 'Hey Aria')
    suppressed = asyncio.Event()
    monkeypatch.setattr(cl, 'emit_turn_trace', lambda recorder: suppressed.set())
    async with Harness(fake) as client:
        ws = await _connect(client, quiet_interval=1)
        await _wake(ws)
        for chunk in _speech_then_silence_pcm():
            await ws.send_bytes(chunk)
        await asyncio.wait_for(suppressed.wait(), timeout=2)
        quiet = await ws.receive_json(timeout=2)
        assert quiet['type'] == 'quiet'
        assert fake.runs == {}
        await ws.close()
