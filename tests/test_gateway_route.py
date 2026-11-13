"""A named device's socket end to end with the converse gateway platform connected: the voice
turn runs in the gateway conversation, never through the Runs API."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("gateway.platforms.base")

from gateway.config import PlatformConfig  # noqa: E402

from hermes_converse.named import DeviceStore  # noqa: E402
from hermes_converse.platform_adapter import ConverseAdapter  # noqa: E402
from tests.test_arbitrate_integration import _patch  # noqa: E402
from tests.test_platform_adapter import FakeStore, NOTIFY, _registered_platform  # noqa: E402,F401
from tests.test_route import API_KEY, Harness, _collect_turn, _speech_then_silence_pcm, _start_msg  # noqa: E402


class Gateway:
    """The route's harness plus a connected converse adapter whose handler plays the runner."""

    def __init__(self, fake, tmp_path, handler):
        self.harness = Harness(fake, device_store=DeviceStore(tmp_path / "devices.db"))
        self.store = FakeStore()
        self.events = []
        self.adapter = ConverseAdapter(PlatformConfig(enabled=True), self.harness.route.hub,
                                       approval_choice=lambda: "deny")
        self.adapter.set_session_store(self.store)

        async def handle(event):
            self.events.append(event)
            return await handler(self, event)

        self.adapter.set_message_handler(handle)

    def session_id(self, event):
        return self.store.peek_session_id(self.adapter._source_session_key(event.source))

    async def __aenter__(self):
        await self.adapter.connect()
        self.client = await self.harness.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self.harness.__aexit__(*exc)
        await self.adapter.cancel_background_tasks()
        await self.adapter.disconnect()

    async def connect(self, **start):
        ws = await self.client.ws_connect("/v1/audio/converse")
        await ws.send_str(_start_msg(key=API_KEY, assistant_name="Arya", device_name="Den Echo",
                                     transcripts=True, **start))
        return ws, await ws.receive_json(timeout=3)


async def _say(ws):
    for chunk in _speech_then_silence_pcm():
        await ws.send_bytes(chunk)
    return await _collect_turn(ws)


async def _streamed(gateway, event):
    sid = gateway.session_id(event)
    gateway.adapter.on_stream_start(session_id=sid)
    gateway.adapter.on_tool_call(session_id=sid)
    for part in ("Hello ", "there."):
        gateway.adapter.on_stream_delta(delta=part, kind="text", session_id=sid)
    return "Hello there."


async def test_voice_turn_runs_in_the_gateway_conversation(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    async with Gateway(fake, tmp_path, _streamed) as gateway:
        ws, ready = await gateway.connect()
        assert ready["type"] == "ready" and ready["device_name"] == "Den Echo" and ready["recipient_id"]
        frames, pcm = await _say(ws)
        kinds = [f["type"] for f in frames]
        assert kinds[:2] == ["transcript", "thinking"] and "speaking" in kinds and kinds[-1] == "turn_done"
        assert pcm
        spoken = next(f for f in frames if f["type"] == "transcript" and f["role"] == "assistant")
        assert spoken["text"] == "Hello there."
        await ws.close()
    (event,) = gateway.events
    assert event.text == "hello"
    assert event.source.chat_name == "Arya: Den Echo" and event.source.user_name == "Den Echo"
    assert event.source.user_id == ready["recipient_id"] and event.source.platform.value == "converse"
    assert "Your name is Arya" in event.channel_prompt and '"device_name": "Den Echo"' in event.channel_prompt
    # Nothing went through the Runs API, and no API-server session was made for the device.
    assert fake.runs == {} and fake.sessions == {}


async def test_reconnect_keeps_the_same_gateway_conversation(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    async with Gateway(fake, tmp_path, _streamed) as gateway:
        ws, first = await gateway.connect()
        await _say(ws)
        await ws.close()
        ws, second = await gateway.connect()
        assert second["recipient_id"] == first["recipient_id"]
        await ws.close()
    assert len(gateway.store.ids) == 1 and gateway.store.switched == []


async def test_earlier_runs_api_conversation_is_continued_once(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    fake.sessions["voice_old"] = {"id": "voice_old", "source": "api_server", "title": "Arya: Den Echo"}
    fake.sessions["voice_tip"] = {"id": "voice_tip", "source": "api_server", "title": "Arya: Den Echo"}
    fake.continuations["voice_old"] = "voice_tip"  # compaction moved the conversation on
    devices = DeviceStore(tmp_path / "devices.db")
    devices.remember(devices.reserve("Arya", "Den Echo"), "voice_old")
    async with Gateway(fake, tmp_path, _streamed) as gateway:
        ws, ready = await gateway.connect()
        assert ready["type"] == "ready"
        await ws.close()
        assert [target for _, target in gateway.store.switched] == ["voice_tip"]
        assert devices.reserve("Arya", "Den Echo").session_id is None
        ws, _ = await gateway.connect()
        await ws.close()
        assert len(gateway.store.switched) == 1
    devices.close()


async def test_deleted_earlier_conversation_starts_fresh(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    devices = DeviceStore(tmp_path / "devices.db")
    devices.remember(devices.reserve("Arya", "Den Echo"), "voice_gone")
    async with Gateway(fake, tmp_path, _streamed) as gateway:
        ws, ready = await gateway.connect()
        assert ready["type"] == "ready" and gateway.store.switched == []
        await ws.close()
    assert devices.reserve("Arya", "Den Echo").session_id is None
    devices.close()


async def test_scheduled_reply_is_spoken_on_the_attached_device(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    async with Gateway(fake, tmp_path, _streamed) as gateway:
        ws, _ = await gateway.connect()
        chat_id = next(iter(gateway.store.ids)).rsplit(":", 1)[-1]
        sent = await gateway.adapter.send(chat_id, "Your timer is done.", metadata=NOTIFY)
        assert sent.success
        frames, pcm = await _collect_turn(ws)
        assert pcm and frames[-1] == {"type": "turn_done", "turn_id": frames[-1]["turn_id"],
                                      "next_action": "standby"}
        assert any(f["type"] == "transcript" and f["text"] == "Your timer is done." for f in frames)
        await ws.close()
        gone = await gateway.adapter.send(chat_id, "Anyone?", metadata=NOTIFY)
        assert not gone.success


async def test_closing_the_socket_stops_the_turn_in_flight(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    interrupted = asyncio.Event()

    async def slow(gateway, event):
        key = gateway.adapter._source_session_key(event.source)
        while not gateway.adapter.has_pending_interrupt(key):
            await asyncio.sleep(0.01)
        interrupted.set()
        return "too late"

    async with Gateway(fake, tmp_path, slow) as gateway:
        ws, _ = await gateway.connect()
        for chunk in _speech_then_silence_pcm():
            await ws.send_bytes(chunk)
        while not gateway.events:
            await asyncio.sleep(0.01)
        await ws.close()
        await asyncio.wait_for(interrupted.wait(), 5)


async def test_without_the_platform_the_device_uses_the_runs_api(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    async with Harness(fake, device_store=DeviceStore(tmp_path / "devices.db")) as client:
        ws = await client.ws_connect("/v1/audio/converse")
        await ws.send_str(_start_msg(key=API_KEY, assistant_name="Arya", device_name="Den Echo"))
        assert (await ws.receive_json(timeout=3))["type"] == "ready"
        await ws.close()
    assert len(fake.sessions) == 1
