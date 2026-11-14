"""Conversation attachment and output delivery over the real authenticated socket/HTTP routes."""
import asyncio
import time

import pytest
from hermes_converse import speech
from tests.support.fake_hermes import FakeHermes
from tests.test_route import (API_KEY, Harness, _FakeStreamer, _patch_speech, _start_msg,
                              _collect_turn, _speech_then_silence_pcm)

HEADERS = {"Authorization": f"Bearer {API_KEY}"}


async def connect(client, **fields):
    ws = await client.ws_connect('/v1/audio/converse')
    await ws.send_str(_start_msg(key=API_KEY, **fields))
    return ws, await ws.receive_json(timeout=2)


async def publish(client, conversation_id, **fields):
    body = {"text": "Your task finished.", "event_id": "event-1",
            "expires_at_unix_ms": (time.time() + 60) * 1000, **fields}
    return await client.post(f'/v1/audio/conversations/{conversation_id}/output',
                             json=body, headers=HEADERS)


@pytest.mark.asyncio
async def test_idle_output_without_mic_wake_or_model_and_reconnect(monkeypatch):
    fake = FakeHermes(API_KEY)
    _patch_speech(monkeypatch, _FakeStreamer([b'\x01\x02']))
    harness = Harness(fake)
    async with harness as client:
        ws, ready = await connect(client)
        cid = ready['conversation_id']
        assert cid == ready['session_id']
        response = await publish(client, cid)
        assert response.status == 202
        frames, pcm = await _collect_turn(ws)
        assert pcm and [f['type'] for f in frames] == ['thinking', 'speaking', 'turn_done']
        assert frames[-1]['next_action'] == 'standby'
        assert not fake.turns  # no synthetic transcript or second LLM call
        duplicate = await publish(client, cid)
        assert duplicate.status == 200 and (await duplicate.json())['status'] == 'duplicate'
        await ws.close()
        for _ in range(100):
            if cid not in harness.route.hub._attachments:
                break
            await asyncio.sleep(.01)
        ws, attached = await connect(client, conversation_id=cid)
        assert attached['type'] == 'ready' and attached['conversation_id'] == cid
        assert (await publish(client, cid, event_id='event-2')).status == 202
        await _collect_turn(ws)
        await ws.close()


@pytest.mark.asyncio
async def test_authorization_attachment_conflicts_and_validation(monkeypatch):
    _patch_speech(monkeypatch, _FakeStreamer([b'\x01\x02']))
    async with Harness(FakeHermes(API_KEY)) as client:
        ws, ready = await connect(client)
        cid = ready['conversation_id']
        second, error = await connect(client, conversation_id=cid)
        assert error['error'] == 'conversation_in_use'
        await second.close()
        missing, error = await connect(client, conversation_id='not-a-conversation')
        assert error['error'] == 'conversation_unavailable'
        await missing.close()
        bad, error = await connect(client, conversation_id='../secret')
        assert error['error'] == 'bad_conversation_id'
        await bad.close()
        denied = await client.post(f'/v1/audio/conversations/{cid}/output', json={})
        assert denied.status == 401
        assert (await publish(client, 'unattached')).status == 409
        assert (await publish(client, cid, expires_at_unix_ms=0)).status == 410
        assert (await publish(client, cid, text='')).status == 400
        assert (await publish(client, cid, next_action='broadcast')).status == 400
        assert (await publish(client, cid, expires_at_unix_ms=10**400)).status == 400
        await ws.close()


@pytest.mark.asyncio
async def test_second_output_waits_for_matching_speaker_drain(monkeypatch):
    _patch_speech(monkeypatch, _FakeStreamer([b'\x01\x02']))
    async with Harness(FakeHermes(API_KEY)) as client:
        ws, ready = await connect(client)
        cid = ready['conversation_id']
        await publish(client, cid)
        frames, _ = await _collect_turn(ws, drain=False)
        await publish(client, cid, event_id='second')
        await ws.send_json({'type': 'drained', 'turn_id': 'stale'})
        with pytest.raises(TimeoutError):
            await ws.receive_json(timeout=.1)
        await ws.send_json({'type': 'drained', 'turn_id': frames[-1]['turn_id']})
        frames, _ = await _collect_turn(ws)
        assert frames[0]['event_id'] == 'second'
        await ws.close()


@pytest.mark.asyncio
async def test_cancel_thinking_stale_id_ignored_and_run_stopped(monkeypatch):
    fake = FakeHermes(API_KEY, deltas=(), hang=True)
    _patch_speech(monkeypatch, _FakeStreamer([b'\x01\x02']))
    async with Harness(fake) as client:
        ws, _ = await connect(client, transcripts=True)
        for frame in _speech_then_silence_pcm():
            await ws.send_bytes(frame)
        transcript = await ws.receive_json(timeout=2)
        assert transcript['type'] == 'transcript'
        assert (await ws.receive_json(timeout=2))['type'] == 'thinking'
        await ws.send_json({'type': 'cancel', 'turn_id': 'stale'})
        with pytest.raises(TimeoutError):
            await ws.receive_json(timeout=.1)
        assert not fake.stopped
        await ws.send_json({'type': 'cancel', 'turn_id': transcript['turn_id']})
        frames, _ = await _collect_turn(ws, timeout=3)
        assert [f['type'] for f in frames] == ['interrupted', 'turn_done']
        assert frames[-1]['next_action'] == 'listen' and fake.stopped
        await ws.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['thinking', 'speaking'])
async def test_voice_continuation_restarts_in_same_conversation(monkeypatch, phase):
    class ContinuationAPI(FakeHermes):
        def begin(self, turn):
            if len(self.turns) > 1:
                self.hang = False
                self.deltas = ('Updated.',)
    fake = ContinuationAPI(API_KEY, deltas=() if phase == 'thinking' else ('Working on the original request.',), hang=True)
    seen = _patch_speech(monkeypatch, _FakeStreamer([b'\x01\x02']))
    def transcribe(path, **kwargs):
        seen['n'] += 1
        return 'first request' if seen['n'] == 1 else 'and the continuation'
    monkeypatch.setattr(speech, 'transcribe_wav', transcribe)
    async with Harness(fake) as client:
        ws, _ = await connect(client, transcripts=True)
        for frame in _speech_then_silence_pcm():
            await ws.send_bytes(frame)
        assert (await ws.receive_json(timeout=2))['type'] == 'transcript'
        assert (await ws.receive_json(timeout=2))['type'] == 'thinking'
        if phase == 'speaking':
            assert (await ws.receive_json(timeout=2))['type'] == 'speaking'
            await ws.receive_bytes(timeout=2)
        for frame in _speech_then_silence_pcm():
            await ws.send_bytes(frame)
        frames, _ = await _collect_turn(ws, timeout=3)
        # A reply cut off while speaking also reports what was said.
        assert [f['type'] for f in frames if f['type'] != 'transcript'] == ['interrupted', 'turn_done']
        following, pcm = await _collect_turn(ws, timeout=3)
        assert [f['type'] for f in following] == ['transcript', 'thinking', 'speaking', 'transcript', 'turn_done']
        assert pcm and len(fake.turns) == 2 and seen['n'] == 2 and fake.stopped == [fake.turns[0]]
        assert len({turn['chat_id'] for turn in fake.turns}) == 1
        assert following[0]['text'] == 'and the continuation'
        await ws.close()


@pytest.mark.asyncio
async def test_expired_queued_output_is_skipped_and_other_outputs_still_run(monkeypatch):
    _patch_speech(monkeypatch, _FakeStreamer([b'\x01\x02']))
    async with Harness(FakeHermes(API_KEY)) as client:
        ws, ready = await connect(client)
        cid = ready['conversation_id']
        await publish(client, cid)
        first, _ = await _collect_turn(ws, drain=False)
        await publish(client, cid, event_id='expires', expires_at_unix_ms=(time.time() + .1) * 1000)
        await publish(client, cid, event_id='valid')
        await asyncio.sleep(.15)
        await ws.send_json({'type': 'drained', 'turn_id': first[-1]['turn_id']})
        frames, _ = await _collect_turn(ws)
        assert frames[0]['event_id'] == 'valid'
        await ws.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('transcript', ['hello', ''])
async def test_output_waits_for_in_progress_capture_even_if_stt_is_empty(monkeypatch, transcript):
    fake = FakeHermes(API_KEY)
    _patch_speech(monkeypatch, _FakeStreamer([b'\x01\x02']))
    monkeypatch.setattr(speech, 'transcribe_wav', lambda *a, **kw: transcript)
    harness = Harness(fake)
    async with harness as client:
        ws, ready = await connect(client, transcripts=True)
        cid = ready['conversation_id']
        audio = _speech_then_silence_pcm()
        for frame in audio[:-60]:
            await ws.send_bytes(frame)
        for _ in range(100):
            if harness.route.hub._attachments[cid].session._capturing:
                break
            await asyncio.sleep(.01)
        assert harness.route.hub._attachments[cid].session._capturing
        assert (await publish(client, cid)).status == 202
        with pytest.raises(TimeoutError):
            await ws.receive_json(timeout=.1)
        for frame in audio[-60:]:
            await ws.send_bytes(frame)
        if transcript:
            first, _ = await _collect_turn(ws)
            assert first[0]['type'] == 'transcript'
        output, pcm = await _collect_turn(ws)
        assert output[0]['event_id'] == 'event-1' and pcm
        assert len(fake.turns) == bool(transcript)
        await ws.close()


@pytest.mark.asyncio
async def test_cancel_after_turn_done_replaces_pending_standby(monkeypatch):
    _patch_speech(monkeypatch, _FakeStreamer([b'\x01\x02']))
    async with Harness(FakeHermes(API_KEY)) as client:
        ws, ready = await connect(client)
        await publish(client, ready['conversation_id'])
        first, _ = await _collect_turn(ws, drain=False)
        turn_id = first[-1]['turn_id']
        assert first[-1]['next_action'] == 'standby'
        await ws.send_json({'type': 'cancel', 'turn_id': turn_id})
        frames, _ = await _collect_turn(ws)
        assert [f['type'] for f in frames] == ['interrupted', 'turn_done']
        assert frames[-1] == {'type': 'turn_done', 'turn_id': turn_id, 'next_action': 'listen'}
        await ws.close()


@pytest.mark.asyncio
async def test_output_capacity_is_bounded_even_after_dispatcher_defers_it():
    import numpy as np
    from hermes_converse.conversation import ConversationHub
    from hermes_converse.loop import ConverseSession
    hub = ConversationHub()
    session = ConverseSession(np)
    attachment = hub.attach('c', session)
    for index in range(32):
        assert hub.publish('c', 'message', str(index), expires_at=time.time()+30) == 'accepted'
        # A dispatcher can remove an event from the queue while deferring its playback.
        session.transcripts.get_nowait()
    assert hub.publish('c', 'overflow', 'extra', expires_at=time.time()+30) == 'busy'
    hub.detach('c', attachment)
    assert hub.publish('c', 'offline', 'offline', expires_at=time.time()+30) == 'not_attached'


@pytest.mark.asyncio
@pytest.mark.parametrize('quiet_interval', [0, 15])
@pytest.mark.parametrize('words', ['CANCEL!', 'stop', 'cancel the kitchen timer'])
async def test_spoken_cancellation_is_distinct_from_continuation(monkeypatch, quiet_interval, words):
    class CorrectedAPI(FakeHermes):
        def begin(self, turn):
            if len(self.turns) > 1:
                self.hang = False
                self.deltas = ('Updated.',)

    fake = CorrectedAPI(API_KEY, deltas=(), hang=True)
    seen = _patch_speech(monkeypatch, _FakeStreamer([b'\x01\x02']))

    def transcribe(path, **kwargs):
        seen['n'] += 1
        return 'turn on the light' if seen['n'] == 1 else words

    monkeypatch.setattr(speech, 'transcribe_wav', transcribe)
    async with Harness(fake) as client:
        ws, _ = await connect(client, quiet_interval=quiet_interval, transcripts=True)
        for frame in _speech_then_silence_pcm():
            await ws.send_bytes(frame)
        assert (await ws.receive_json(timeout=2))['type'] == 'transcript'
        assert (await ws.receive_json(timeout=2))['type'] == 'thinking'
        for frame in _speech_then_silence_pcm():
            await ws.send_bytes(frame)
        old, _ = await _collect_turn(ws)
        assert [f['type'] for f in old] == ['interrupted', 'turn_done']
        following, _ = await _collect_turn(ws)
        assert following[0]['text'] == words
        assert len(fake.turns) == 2 and fake.turns[1]['text'] == words
        assert fake.stopped == [fake.turns[0]]
        await ws.close()


@pytest.mark.asyncio
async def test_tail_barge_captures_continuation_before_old_drain(monkeypatch):
    fake = FakeHermes(API_KEY)
    seen = _patch_speech(monkeypatch, _FakeStreamer([b'\x01\x02']))

    def transcribe(path, **kwargs):
        seen['n'] += 1
        return 'original request' if seen['n'] == 1 else 'additional idea'

    monkeypatch.setattr(speech, 'transcribe_wav', transcribe)
    async with Harness(fake) as client:
        ws, _ = await connect(client, transcripts=True)
        for frame in _speech_then_silence_pcm():
            await ws.send_bytes(frame)
        original, _ = await _collect_turn(ws, drain=False)
        for frame in _speech_then_silence_pcm():
            await ws.send_bytes(frame)
        interrupted, _ = await _collect_turn(ws, drain=False)
        assert [f['type'] for f in interrupted] == ['interrupted', 'turn_done']
        assert interrupted[-1]['turn_id'] == original[-1]['turn_id']
        await ws.send_json({'type': 'drained', 'turn_id': original[-1]['turn_id']})
        following, pcm = await _collect_turn(ws)
        assert following[0]['text'] == 'additional idea' and pcm
        assert following[0]['turn_id'] != original[-1]['turn_id']
        assert len(fake.turns) == 2
        await ws.close()
