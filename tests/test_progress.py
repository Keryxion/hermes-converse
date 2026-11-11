"""Long-turn keepalives are correlated, bounded by the turn, and safe to ignore."""
import asyncio

import pytest
from aiohttp import WSMsgType

from hermes_converse import loop as cl, tracing
from tests.test_arbitrate_integration import _connect, _patch, _speech_seams
from tests.test_loop import _EchoSynth, _FakeConverseSession
from tests.test_route import Harness, _speech_then_silence_pcm


@pytest.fixture(autouse=True)
def fast_progress(monkeypatch):
    monkeypatch.setattr(cl, 'THINKING_FRAME_INTERVAL', 0.01)


async def _next_frame(ws):
    while True:
        message = await ws.receive()
        if message.type == WSMsgType.TEXT:
            return message.json()
        assert message.type == WSMsgType.BINARY


def _progress_tasks():
    return [task for task in asyncio.all_tasks()
            if task.get_coro().__name__ == '_report_thinking']


@pytest.mark.parametrize('outcome', ['ok', 'error', 'exception', 'timeout', 'disconnect'])
@pytest.mark.asyncio
async def test_progress_lives_only_as_long_as_its_turn(monkeypatch, outcome):
    frames = []
    two_keepalives = asyncio.Event()
    run_cancelled = asyncio.Event()
    session = _FakeConverseSession(['hello'])
    traceparent = '00-' + '1' * 32 + '-' + '2' * 16 + '-01'

    def start_trace(**kwargs):
        turn = tracing.TurnTrace()
        turn.traceparent = traceparent
        return turn

    monkeypatch.setattr(cl, 'start_turn', start_trace)

    async def send_json(frame):
        frames.append(frame)
        if sum(f['type'] == 'thinking' for f in frames) >= 3:
            two_keepalives.set()

    async def send_bytes(data):
        pass

    async def run_turn(transcript, on_delta, *, interrupted):
        try:
            await two_keepalives.wait()
            if outcome in ('timeout', 'disconnect'):
                await asyncio.Event().wait()
            if outcome == 'exception':
                raise RuntimeError('test failure')
            if outcome == 'error':
                return '', 'test failure'
            on_delta('Hello.')
            return 'Hello.', None
        except asyncio.CancelledError:
            run_cancelled.set()
            raise

    driver = asyncio.create_task(cl.drive_converse_turns(
        session=session, synth=_EchoSynth(), cap=4000, loop=asyncio.get_running_loop(),
        send_json=send_json, send_bytes=send_bytes, run_turn=run_turn, history=[], turn_timeout=0.08))
    try:
        async with asyncio.timeout(2):
            await two_keepalives.wait()
            if outcome == 'disconnect':
                driver.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await driver
            else:
                await driver
        keepalives = [f for f in frames if f['type'] == 'thinking']
        assert len(keepalives) >= 3
        assert all(f['turn_id'] == frames[0]['turn_id'] and f['traceparent'] == traceparent for f in keepalives)
        if outcome != 'disconnect':
            terminal = next(i for i, f in enumerate(frames) if f['type'] in ('error', 'turn_done'))
            assert not any(f['type'] == 'thinking' for f in frames[terminal:])
            assert frames[-1]['type'] == 'turn_done'
        if outcome in ('timeout', 'disconnect'):
            assert run_cancelled.is_set()
        assert not _progress_tasks()
        count = len(frames)
        await asyncio.sleep(0.03)
        assert len(frames) == count
    finally:
        driver.cancel()
        await asyncio.gather(driver, return_exceptions=True)


@pytest.mark.asyncio
async def test_short_turn_has_no_keepalive(monkeypatch):
    monkeypatch.setattr(cl, 'THINKING_FRAME_INTERVAL', 5.0)
    frames = []

    async def run_turn(transcript, on_delta, *, interrupted):
        return '', None

    async def send_json(frame):
        frames.append(frame)

    async def send_bytes(data):
        pass

    await cl.drive_converse_turns(
        session=_FakeConverseSession(['hello']), synth=_EchoSynth(), cap=4000,
        loop=asyncio.get_running_loop(), send_json=send_json, send_bytes=send_bytes,
        run_turn=run_turn, history=[])
    assert [f['type'] for f in frames].count('thinking') == 1
    assert not _progress_tasks()


@pytest.mark.asyncio
async def test_progress_over_websocket_does_not_extend_server_timeout(monkeypatch):
    fake = _patch(monkeypatch)
    fake.hang = True
    fake.pre_events = (('tool.started', {'tool_call_id': 'a', 'tool': 'read_file'}),)
    async with Harness(fake, settings={'turn_timeout_seconds': 0.12}) as client:
        ws = await _connect(client)
        for chunk in _speech_then_silence_pcm():
            await ws.send_bytes(chunk)
        frames = []
        async with asyncio.timeout(2):
            while not frames or frames[-1]['type'] != 'turn_done':
                frames.append(await _next_frame(ws))
        thinking = [f for f in frames if f['type'] == 'thinking']
        assert len(thinking) >= 3
        assert thinking[0]['tool_calls'] == 0
        assert any(frame['tool_calls'] == 1 for frame in thinking[1:])
        assert all(f['turn_id'] == frames[0]['turn_id'] for f in thinking)
        assert any(f['type'] == 'error' and 'timed out' in f['error'] for f in frames)
        with pytest.raises(TimeoutError):
            await ws.receive_json(timeout=0.04)
        await ws.close()


@pytest.mark.asyncio
async def test_cancelled_turn_stops_keepalives(monkeypatch):
    fake = _patch(monkeypatch)
    fake.hang = True
    async with Harness(fake) as client:
        ws = await _connect(client)
        for chunk in _speech_then_silence_pcm():
            await ws.send_bytes(chunk)
        async with asyncio.timeout(2):
            thinking_count = 0
            while thinking_count < 2:
                thinking_count += (await _next_frame(ws))['type'] == 'thinking'
            await ws.send_json({'type': 'cancel'})
            while (await _next_frame(ws))['type'] != 'turn_done':
                pass
        with pytest.raises(TimeoutError):
            await ws.receive_json(timeout=0.04)
        await ws.close()


@pytest.mark.asyncio
async def test_thinking_reports_tool_count_during_speech_and_resets_each_turn():
    frames = []
    updates = asyncio.Queue()
    count = 99  # A previous run's count must not leak into the initial frame.

    async def send_json(frame):
        frames.append(frame)
        if frame['type'] == 'thinking':
            updates.put_nowait(frame)

    async def send_bytes(data):
        pass

    async def run_turn(transcript, on_delta, *, interrupted):
        nonlocal count
        count = 0
        assert (await updates.get())['tool_calls'] == 0
        count = 2
        on_delta('Hello.')
        while True:
            update = await updates.get()
            if any(f['type'] == 'speaking' and f['turn_id'] == update['turn_id'] for f in frames):
                assert update['tool_calls'] == 2
                break
        count = 3
        assert (await updates.get())['tool_calls'] == 3
        return 'Hello.', None

    async with asyncio.timeout(2):
        await cl.drive_converse_turns(
            session=_FakeConverseSession(['first', 'second']), synth=_EchoSynth(), cap=4000,
            loop=asyncio.get_running_loop(), send_json=send_json, send_bytes=send_bytes,
            run_turn=run_turn, history=[], tool_calls=lambda: count)
    assert sum(f['type'] == 'turn_done' for f in frames) == 2
    assert not any(f['type'] == 'working' for f in frames)
    assert not _progress_tasks()
