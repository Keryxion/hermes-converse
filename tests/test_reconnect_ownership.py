"""Reconnect must keep microphone control with the turn producing reply audio."""
import asyncio

import numpy as np
import pytest
from aiohttp import web

from hermes_converse import route, speech
from tests.test_gateway_route import _connect
from tests.test_arbitrate_integration import _patch
from tests.test_route import Harness, _FakeStreamer, _collect_turn, _patch_speech, _speech_then_silence_pcm


async def _wait_for(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(.01)


@pytest.mark.parametrize('input_rate', [16000, 8000])
@pytest.mark.parametrize('action', ['speech', 'cancel', 'drain'])
async def test_reconnected_microphone_controls_the_running_audio(monkeypatch, action, input_rate):
    fake = _patch(monkeypatch)
    drivers = []
    recordings = []

    class Recorder:
        def __init__(self, session):
            self.frames = []
            self.closed = False
            recordings.append(self)

        def audio(self, data):
            pass

        def frame(self, frame):
            assert not self.closed
            self.frames.append(frame)

        def close(self):
            self.closed = True

        async def wait_closed(self):
            pass

    monkeypatch.setattr(route.diagnostic, 'ServerRecording', Recorder)
    drive = route.drive_converse_turns
    async def tracked_driver(**kwargs):
        drivers.append(kwargs['session'])
        await drive(**kwargs)
    monkeypatch.setattr(route, 'drive_converse_turns', tracked_driver)
    # Provider seams only: the actual socket, input worker, detector and driver run.
    streamer = _FakeStreamer([np.full(2400, 3000, dtype=np.int16).tobytes()] * 50)
    _patch_speech(monkeypatch, streamer)
    words = iter(['hello', 'and dim it'])
    monkeypatch.setattr(speech, 'transcribe_wav', lambda *a, **kw: next(words, ''))
    fake.hang = True
    harness = Harness(fake)
    async with harness as client:
        ws, _ = await _connect(client)
        await ws.send_bytes(b''.join(_speech_then_silence_pcm()))
        original = next(iter(harness.route.hub._attachments.values())).session
        async with asyncio.timeout(20):
            while True:
                msg = await ws.receive()
                if msg.type == web.WSMsgType.BINARY:
                    break
        turn_id = original._active_turn_id
        assert turn_id and original._playing.is_set()
        await ws.close()
        await _wait_for(lambda: harness.route.hub.detached)
        ws, ready = await _connect(client, input_rate=input_rate)
        assert ready['type'] == 'ready'
        # A second loss must transfer the same owner, rather than stack another writer.
        await ws.close()
        await _wait_for(lambda: harness.route.hub.detached)
        ws, ready = await _connect(client, input_rate=input_rate)
        assert ready['type'] == 'ready'
        assert next(iter(harness.route.hub._attachments.values())).session is original
        assert drivers == [original]
        if action == 'speech':
            await ws.send_bytes(b''.join(_speech_then_silence_pcm(block=input_rate * 30 // 1000)))
            async with asyncio.timeout(8):
                frames, _ = await _collect_turn(ws)
            assert any(f['type'] == 'interrupted' and f['turn_id'] == turn_id for f in frames)
            assert frames[-1]['turn_id'] == turn_id
            fake.hang = False
            async with asyncio.timeout(20):
                continuation, pcm = await _collect_turn(ws)
            assert continuation[-1]['turn_id'] != turn_id and pcm
            # Hermes may redirect the original run or admit a new one after it settles.
            assert sum((turn['text'] == 'and dim it') + turn['redirects'].count('and dim it')
                       for turn in fake.turns) == 1
            assert drivers == [original]
        elif action == 'cancel':
            await ws.send_json({'type': 'cancel', 'turn_id': turn_id})
            async with asyncio.timeout(8):
                frames, _ = await _collect_turn(ws)
            assert any(f['type'] == 'interrupted' and f['turn_id'] == turn_id for f in frames)
            first, second, current = recordings
            assert first.closed and second.closed
            assert any(f['type'] == 'interrupted' and f['turn_id'] == turn_id for f in current.frames)
        else:
            reports = []
            ended = original.playback_ended
            def report(value):
                reports.append(value)
                ended(value)
            monkeypatch.setattr(original, 'playback_ended', report)
            await ws.send_json({'type': 'state', 'turn_id': turn_id, 'state': 'listening'})
            await _wait_for(lambda: turn_id in reports)
        fake.hang = False
        await ws.send_json({'stop': True})
        await ws.close()


async def test_reconnect_after_turn_done_keeps_playback_drain_owner(monkeypatch):
    fake = _patch(monkeypatch)
    harness = Harness(fake, {"playback_tail_timeout_seconds": 10})
    async with harness as client:
        ws, _ = await _connect(client)
        original = next(iter(harness.route.hub._attachments.values())).session
        await ws.send_bytes(b''.join(_speech_then_silence_pcm()))
        frames, _ = await _collect_turn(ws, drain=False)
        turn_id = frames[-1]['turn_id']
        assert original._tail_active
        await ws.close()
        await _wait_for(lambda: harness.route.hub.detached)
        ws, ready = await _connect(client)
        assert ready['type'] == 'ready'
        assert next(iter(harness.route.hub._attachments.values())).session is original
        await ws.send_json({'type': 'state', 'turn_id': turn_id, 'state': 'listening'})
        await _wait_for(lambda: not original._tail_active)
        await ws.send_json({'stop': True})
        await ws.close()


async def test_subsequent_turns_use_new_socket_model_codec_and_controls(monkeypatch):
    from hermes_converse.audio_transport import AudioTransport
    from tests.support.converse_audio import speech_like
    fake = _patch(monkeypatch)
    _patch_speech(monkeypatch, _FakeStreamer([np.full(2400, 3000, dtype=np.int16).tobytes()]))
    words = iter(['first', 'second', 'third'])
    monkeypatch.setattr(speech, 'transcribe_wav', lambda *a, **kw: next(words, ''))
    fake.hang = True
    harness = Harness(fake)
    incoming = AudioTransport('opus', 8000, 8000)
    try:
        async with harness as client:
            ws, _ = await _connect(client)
            await ws.send_bytes(b''.join(_speech_then_silence_pcm()))
            async with asyncio.timeout(20):
                while (await ws.receive()).type != web.WSMsgType.BINARY:
                    pass
            original = next(iter(harness.route.hub._attachments.values())).session
            await ws.close()
            await _wait_for(lambda: harness.route.hub.detached)
            ws, ready = await _connect(client, input_rate=8000, output_rate=16000,
                                       audio_format='opus', quiet_interval=.3)
            assert ready['output']['format'] == 'opus'
            assert original._quiet_interval == .3
            await ws.send_json({'type': 'cancel'})
            await _collect_turn(ws)
            await ws.send_json({'type': 'command', 'name': 'model', 'args': ['next-model', '--session']})
            async with asyncio.timeout(5):
                while (reply := await ws.receive_json())['type'] != 'command':
                    pass
            assert reply['current_model']['model'] == 'next-model'
            fake.hang = False
            # Opus filters a DC step; use speech-shaped PCM through the real codec.
            pcm = np.concatenate([np.zeros(4800, dtype=np.int16),
                                  speech_like(9000, seconds=.8, rate=8000),
                                  np.zeros(19200, dtype=np.int16)]).astype('<i2').tobytes()
            for packet in incoming.encode(pcm, 8000):
                await ws.send_bytes(packet)
            frames, packets = await _collect_turn(ws)
            assert packets and fake.turns[-1]['text'] == 'second'
            assert fake.turns[-1]['model'] == 'next-model'
            fake.hang = True
            for packet in incoming.encode(pcm, 8000):
                await ws.send_bytes(packet)
            await _wait_for(lambda: any(t['text'] == 'third' for t in fake.turns))
            await ws.send_json({'type': 'cancel'})
            frames, _ = await _collect_turn(ws)
            assert any(f['type'] == 'interrupted' for f in frames)
            await ws.send_json({'stop': True})
            await ws.close()
    finally:
        fake.hang = False
        incoming.close()
