"""Voice route lifecycle and wake transport without network sockets or model calls."""
import asyncio
import queue
import threading
from types import SimpleNamespace

import pytest

from hermes_converse.conversation import ConversationHub
from hermes_converse import route
from hermes_converse.settings import Settings


@pytest.mark.asyncio
async def test_route_runs_wake_without_streaming_and_cancels_on_disconnect(monkeypatch):
    disconnected = asyncio.Event()
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    calls = []
    hub = ConversationHub()

    class Socket:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await disconnected.wait()
            raise StopAsyncIteration

        async def send_json(self, frame):
            pass

        async def send_bytes(self, data):
            raise AssertionError('wake deltas must not go straight to TTS')

        async def close(self, **kwargs):
            pass

    class Client:
        async def run_turn(self, **kwargs):
            calls.append(kwargs)
            kwargs['on_delta']('Raw text before the silent verdict')
            entered.set()
            if len(calls) == 2:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            hub.on_response(session_id='voice', turn_id='wake', assistant_response='[SILENT]')
            return SimpleNamespace(error=None, status='completed', reply='[SILENT]')

        async def stop(self):
            pass

    class Session:
        session_id = 'voice'
        stopped = False
        _turn_active = threading.Event()
        _tail_active = False
        transcripts = queue.Queue()

        def enable_turn_control(self):
            pass

        def start(self):
            pass

        def stop(self):
            self.stopped = True

        def capture_pending(self):
            return False

    async def driver(**kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(route, 'drive_converse_turns', driver)
    session = Session()
    attachment = hub.attach('voice', session)
    task = asyncio.create_task(route._serve(Socket(), session=session, synth=None, cap=None,
        client=Client(), settings=Settings(), name='Arya', quiet_interval=0,
        hub=hub, attachment=attachment))
    try:
        for _ in range(8):
            await asyncio.sleep(0)
        assert hub.inject_wake('voice', 'first wake')
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(8):
            await asyncio.sleep(0)
        assert session.transcripts.empty()
        assert calls[0]['session_id'] == 'voice'
        assert calls[0]['idempotency_key'].startswith('pulse-')
        assert 'spoken aloud' in calls[0]['instructions']
        entered.clear()
        assert hub.inject_wake('voice', 'second wake')
        await asyncio.wait_for(entered.wait(), 1)
        disconnected.set()
        await asyncio.wait_for(task, 1)
        assert cancelled.is_set()
        assert not hub.inject_wake('voice', 'offline wake')
        assert not attachment.wake_tasks
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
