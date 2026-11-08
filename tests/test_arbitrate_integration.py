"""Real WebSocket and driver coverage for the cooperative wake contract.

Only speech-provider seams are replaced; no Hermes checkout is required.
"""
import asyncio
import re
import threading

import pytest
from aiohttp import web

from hermes_converse import speech
from tests.support.fake_api import FakeRunsAPI
from tests.test_route import (
    Harness, _FakeStreamer, _patch_speech, _collect_turn, _speech_then_silence_pcm, _start_msg,
)

@pytest.fixture(autouse=True)
def _speech_seams(monkeypatch):
    """Replace the Hermes speech seams the driver uses with in-process equivalents."""

    class _Chunker:
        def __init__(self) -> None:
            self.buf = ""

        def feed(self, delta: str):
            self.buf += delta
            out = []
            while True:
                cuts = [i for i in (self.buf.find("."), self.buf.find("!"), self.buf.find("?")) if i >= 0]
                if not cuts:
                    break
                cut = min(cuts)
                out.append(self.buf[: cut + 1])
                self.buf = self.buf[cut + 1:]
            return out

        def flush(self):
            out = [self.buf] if self.buf.strip() else []
            self.buf = ""
            return out

    def _spawn(target, *, name, args=()):
        return threading.Thread(target=target, name=name, args=args, daemon=True)

    monkeypatch.setattr(speech, "spawn_context_thread", _spawn)
    monkeypatch.setattr(speech, "sentence_chunker", lambda: _Chunker())
    monkeypatch.setattr(speech, "strip_markdown", lambda text: text)
    monkeypatch.setattr(speech, "sentence_boundary_re", lambda: re.compile(r"(?<=[.!?])\s+"))
    monkeypatch.setattr(speech, "mark_speech_interrupted", lambda: None)


async def _connect(client):
    ws = await client.ws_connect('/v1/audio/converse')
    await ws.send_str(_start_msg(key='-'.join(('fixture', 'converse', 'api', 'key', '0123456789'))))
    assert (await ws.receive_json(timeout=2))['type'] == 'ready'
    return ws


def _patch(monkeypatch):
    streamer = _FakeStreamer([b"\x01\x02\x03\x04"])
    _patch_speech(monkeypatch, streamer)
    monkeypatch.setattr(speech, 'transcribe_wav', lambda *a, **kw: 'hello')
    return FakeRunsAPI('-'.join(('fixture', 'converse', 'api', 'key', '0123456789')), deltas=('Hello.',))


async def _audio_turn(ws):
    for chunk in _speech_then_silence_pcm():
        await ws.send_bytes(chunk)
    frames, pcm = await _collect_turn(ws)
    assert any(f['type'] == 'transcript' for f in frames)
    assert any(f['type'] == 'thinking' and f['turn_id'] for f in frames)
    assert any(f['type'] == 'speaking' for f in frames)
    assert pcm
    return frames


@pytest.mark.asyncio
async def test_disabled_echoes_client_id(monkeypatch):
    async with Harness(_patch(monkeypatch)) as client:
        ws = await _connect(client)
        await ws.send_json({'type': 'wake', 'claim_id': 'client-1', 'wake_word': 'Hey Arya'})
        assert await ws.receive_json(timeout=1) == {
            'type': 'wake_grant', 'claim_id': 'client-1', 'wake_word': 'Hey Arya'}
        await ws.close()


@pytest.mark.parametrize('first_signals,second_signals', [
    ({'confidence': 1, 'volume': 0.5}, {'confidence': 0.5, 'volume': 1}),
    ({'confidence': 0.9, 'volume': 0.8}, {'confidence': 0.9, 'volume': 0.2}),
])
@pytest.mark.asyncio
async def test_denial_is_advisory_and_followups_run(monkeypatch, first_signals, second_signals):
    fake = _patch(monkeypatch)
    async with Harness(fake, settings={'arbitrate_enabled': True,
                                        'arbitrate_device_id': 'shared-label'}) as client:
        first, second = await _connect(client), await _connect(client)
        await first.send_json({'type': 'wake', 'claim_id': 'first', 'wake_word': 'Hey Arya',
                               **first_signals})
        await second.send_json({'type': 'wake', 'claim_id': 'second', 'wake_word': 'Hey Arya',
                                **second_signals})
        grant, deny = await asyncio.gather(first.receive_json(timeout=1), second.receive_json(timeout=1))
        assert grant['type'] == 'wake_grant' and grant['claim_id'] == 'first'
        assert deny['type'] == 'wake_deny' and deny['claim_id'] == 'second'
        assert deny['winner_device_id'].startswith('shared-label_')
        # A client ignoring the advice still gets a full answer, and so does its follow-up.
        await _audio_turn(second)
        await _audio_turn(second)
        assert len(fake.runs) == 2
        assert len({run['body']['session_id'] for run in fake.runs.values()}) == 1
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_clients_without_wake_both_answer_same_transcript(monkeypatch):
    fake = _patch(monkeypatch)
    async with Harness(fake, settings={'arbitrate_enabled': True}) as client:
        first, second = await _connect(client), await _connect(client)
        await asyncio.gather(_audio_turn(first), _audio_turn(second))
        assert len(fake.runs) == 2
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_overlapping_claims_keep_ids_and_exact_phrases(monkeypatch):
    async with Harness(_patch(monkeypatch), settings={'arbitrate_enabled': True}) as client:
        ws = await _connect(client)
        await ws.send_json({'type': 'wake', 'claim_id': 'old', 'wake_word': 'Arya'})
        await ws.send_json({'type': 'wake', 'claim_id': 'new', 'wake_word': 'Hey Arya'})
        replies = [await ws.receive_json(timeout=1), await ws.receive_json(timeout=1)]
        assert {r['claim_id']: r['wake_word'] for r in replies} == {'old': 'Arya', 'new': 'Hey Arya'}
        assert all(r['type'] == 'wake_grant' for r in replies)
        await ws.close()


@pytest.mark.asyncio
async def test_pending_wake_does_not_block_audio_or_stop(monkeypatch):
    fake = _patch(monkeypatch)
    async with Harness(fake, settings={'arbitrate_enabled': True,
                                        'arbitrate_wake_window_seconds': 5}) as client:
        ws = await _connect(client)
        await ws.send_json({'type': 'wake', 'claim_id': 'slow', 'wake_word': 'Hey Arya'})
        # Audio is accepted while a decision is pending; no wake grant is required.
        await asyncio.wait_for(_audio_turn(ws), timeout=2)
        await ws.send_json({'stop': True})
        msg = await ws.receive(timeout=1)
        assert msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSED)
        await ws.close()


@pytest.mark.asyncio
async def test_invalid_wake_does_not_close_socket(monkeypatch):
    async with Harness(_patch(monkeypatch)) as client:
        ws = await _connect(client)
        await ws.send_json({'type': 'wake', 'wake_word': 'Arya'})
        assert (await ws.receive_json(timeout=1))['error'] == 'bad_wake'
        await _audio_turn(ws)
        await ws.close()


@pytest.mark.asyncio
async def test_pending_wake_does_not_block_control_frames(monkeypatch):
    from hermes_converse.loop import ConverseSession

    seen = {name: asyncio.Event() for name in ('cancel', 'commit', 'notify_drained')}
    for name, event in seen.items():
        original = getattr(ConverseSession, name)
        def record(self, *args, _original=original, _event=event):
            _event.set()
            return _original(self, *args)
        monkeypatch.setattr(ConverseSession, name, record)
    async with Harness(_patch(monkeypatch), settings={'arbitrate_enabled': True,
                                                     'arbitrate_wake_window_seconds': 5}) as client:
        ws = await _connect(client)
        await ws.send_json({'type': 'wake', 'claim_id': 'slow', 'wake_word': 'Hey Arya'})
        await ws.send_json({'type': 'cancel'})
        await ws.send_json({'commit': True})
        await ws.send_json({'type': 'drained'})
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in seen.values())), timeout=1)
        await ws.close()


@pytest.mark.parametrize('spacing,second_phrase,expected', [
    (0.35, 'Hey Arya', ['wake_deny', 'wake_grant']),
    (0.70, 'Hey Arya', ['wake_deny', 'wake_grant']),
    (0.35, 'Different assistant', ['wake_grant', 'wake_grant']),
    (0.85, 'Hey Arya', ['wake_grant', 'wake_grant']),
])
@pytest.mark.asyncio
async def test_default_window_over_websockets(monkeypatch, spacing, second_phrase, expected, record_property):
    """Audio-free diagnostic: each deadline starts at that client's own send."""
    fake = _patch(monkeypatch)
    async with Harness(fake, settings={'arbitrate_enabled': True}) as client:
        first, second = await _connect(client), await _connect(client)
        clock = asyncio.get_running_loop().time

        async def claim(ws, claim_id, phrase, confidence):
            sent = clock()
            async with asyncio.timeout_at(sent + 1.0):
                await ws.send_json({'type': 'wake', 'claim_id': claim_id, 'wake_word': phrase,
                                    'confidence': confidence})
                reply = await ws.receive_json()
            return reply, clock() - sent

        first_task = asyncio.create_task(claim(first, 'first', 'Hey Arya', 0.5))
        try:
            await asyncio.sleep(spacing)
            results = await asyncio.gather(first_task, claim(second, 'second', second_phrase, 1))
            assert [r['type'] for r, _ in results] == expected
            assert [r['claim_id'] for r, _ in results] == ['first', 'second']
            assert [r['wake_word'] for r, _ in results] == ['Hey Arya', second_phrase]
            for index, (_, latency) in enumerate(results):
                record_property(f'claim_{index}_latency_ms', round(latency * 1000, 1))
            assert not fake.runs  # No audio, agent run, or TTS needed to reproduce the election.
        finally:
            first_task.cancel()
            await asyncio.gather(first_task, return_exceptions=True)
            await first.close()
            await second.close()
