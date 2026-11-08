"""Voice telemetry follows real capture/driver/WebSocket lifetimes."""
import asyncio
import threading

import pytest

from hermes_converse import route, tracing
from tests.test_arbitrate_integration import _audio_turn, _connect, _patch, _speech_seams
from tests.test_route import Harness, _speech_then_silence_pcm
from tests.test_tracing import otel


@pytest.mark.asyncio
async def test_wire_traceparent_matches_exported_turn(monkeypatch, otel):
    _, _, exporter = otel
    async with Harness(_patch(monkeypatch)) as client:
        ws = await _connect(client)
        frames = await _audio_turn(ws)
        await ws.close()
    spans = exporter.get_finished_spans()
    parent = next(s for s in spans if s.name == 'voice.turn' and s.attributes.get('outcome') == 'ok')
    assert {s.name for s in spans} >= {'voice.turn', 'voice.capture', 'voice.stt', 'voice.agent', 'voice.tts'}
    capture = next(s for s in spans if s.name == 'voice.capture')
    assert capture.attributes['input.audio_ms'] > capture.attributes['endpoint.silence_ms'] > 0
    for frame in frames:
        if frame['type'] in ('transcript', 'thinking', 'speaking'):
            assert int(frame['traceparent'].split('-')[1], 16) == parent.context.trace_id
            assert int(frame['traceparent'].split('-')[2], 16) == parent.context.span_id
            assert frame['turn_id'] == parent.attributes['turn_id']
    assert all(s.parent.span_id == parent.context.span_id for s in spans if s.name != 'voice.turn')
    assert 'hello' not in repr([dict(s.attributes) for s in spans])


@pytest.mark.asyncio
async def test_disconnect_closes_live_turn(monkeypatch, otel):
    _, _, exporter = otel
    fake = _patch(monkeypatch)
    fake.hang = True
    async with Harness(fake) as client:
        ws = await _connect(client)
        for chunk in _speech_then_silence_pcm():
            await ws.send_bytes(chunk)
        async with asyncio.timeout(2):
            while (await ws.receive_json())['type'] != 'thinking':
                pass
        await ws.close()
    parents = [s for s in exporter.get_finished_spans() if s.name == 'voice.turn']
    assert len(parents) == 1
    assert parents[0].attributes['outcome'] == 'interrupted'
    assert parents[0].attributes['interrupted'] is True


@pytest.mark.asyncio
async def test_app_cleanup_flushes_off_event_loop(monkeypatch):
    calls = []
    main_thread = threading.get_ident()
    monkeypatch.setattr(route, 'force_flush', lambda: calls.append(threading.get_ident()))
    async with Harness(_patch(monkeypatch)):
        pass
    assert len(calls) == 1 and calls[0] != main_thread


def test_smart_turn_events_include_hold_commit_and_budget(otel):
    from hermes_converse import vad
    from tests.test_loop import _ScriptedDetector, _adaptive_session, _feed_silence

    _, _, exporter = otel
    for probabilities, reason in [([0.1, 0.9], 'threshold'), ([0.0], 'hold_budget')]:
        recorder = tracing.PhaseRecorder()
        session = _adaptive_session(_ScriptedDetector(probabilities), max_blocks=500)
        _feed_silence(session, session._endpoint_blocks * 6 + 4)
        with recorder.phase('voice.capture', **{'endpoint.mode': 'smart_turn'}) as phase:
            wav, _ = session._capture_adaptive(float(vad.SILENCE_RMS_THRESHOLD), phase=phase)
        vad.unlink_quietly(wav)
        tracing.start_turn(recorder=recorder, turn_id=reason).end()
        capture = [s for s in exporter.get_finished_spans() if s.name == 'voice.capture'][-1]
        decisions = [e.attributes for e in capture.events]
        assert decisions[0]['endpoint.decision'] == 'hold'
        assert decisions[-1]['endpoint.decision'] == 'commit'
        assert decisions[-1]['endpoint.reason'] == reason
        assert all(0 <= d['endpoint.probability'] <= 1 for d in decisions)
