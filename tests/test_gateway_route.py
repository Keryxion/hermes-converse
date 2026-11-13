"""A socket end to end with its voice turns running in a gateway conversation."""

from __future__ import annotations

import asyncio
import sqlite3

from hermes_converse.named import DeviceStore
from tests.test_arbitrate_integration import _patch
from tests.test_route import API_KEY, Harness, _collect_turn, _speech_then_silence_pcm, _start_msg

NOTIFY = {"notify": True}


async def _connect(client, **start):
    ws = await client.ws_connect("/v1/audio/converse")
    start.setdefault("assistant_name", "Arya")
    start.setdefault("device_name", "Den Echo")
    await ws.send_str(_start_msg(key=API_KEY, transcripts=True, **{k: v for k, v in start.items() if v}))
    return ws, await ws.receive_json(timeout=3)


async def _say(ws):
    for chunk in _speech_then_silence_pcm():
        await ws.send_bytes(chunk)
    return await _collect_turn(ws)


def _earlier_conversation(path, session_id):
    """A device record as the plugin left it when conversations were API sessions."""
    devices = DeviceStore(path)
    device = devices.reserve("Arya", "Den Echo")
    devices.close()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE conversations SET session_id=? WHERE recipient_id=?", (session_id, device.recipient_id))


async def test_voice_turn_runs_in_the_gateway_conversation(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    async with Harness(fake, device_store=DeviceStore(tmp_path / "devices.db")) as client:
        ws, ready = await _connect(client)
        assert ready["type"] == "ready" and ready["device_name"] == "Den Echo" and ready["recipient_id"]
        frames, pcm = await _say(ws)
        kinds = [f["type"] for f in frames]
        assert kinds[:2] == ["transcript", "thinking"] and "speaking" in kinds and kinds[-1] == "turn_done"
        assert pcm
        spoken = next(f for f in frames if f["type"] == "transcript" and f["role"] == "assistant")
        assert spoken["text"] == "Hello."
        await ws.close()
    (turn,) = fake.turns
    assert turn["text"] == "hello" and turn["status"] == "completed"
    assert turn["chat_name"] == "Arya: Den Echo" and turn["user_name"] == "Den Echo"
    assert turn["user_id"] == ready["recipient_id"]
    assert "Your name is Arya" in turn["instructions"] and '"device_name": "Den Echo"' in turn["instructions"]


async def test_reconnect_keeps_the_same_gateway_conversation(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    async with Harness(fake, device_store=DeviceStore(tmp_path / "devices.db")) as client:
        ws, first = await _connect(client)
        await _say(ws)
        await ws.close()
        ws, second = await _connect(client)
        assert second["recipient_id"] == first["recipient_id"]
        await ws.close()
    assert len(fake.store.ids) == 1 and fake.store.switched == []


async def test_earlier_api_conversation_is_continued_once(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    fake.sessions["voice_old"] = {"id": "voice_old"}
    fake.continuations["voice_old"] = "voice_tip"  # compaction moved the conversation on
    _earlier_conversation(tmp_path / "devices.db", "voice_old")
    devices = DeviceStore(tmp_path / "devices.db")
    async with Harness(fake, device_store=devices) as client:
        ws, ready = await _connect(client)
        assert ready["type"] == "ready"
        await ws.close()
        assert [target for _, target in fake.store.switched] == ["voice_tip"]
        assert devices.reserve("Arya", "Den Echo").session_id is None
        ws, _ = await _connect(client)
        await ws.close()
        assert len(fake.store.switched) == 1


async def test_deleted_earlier_conversation_starts_fresh(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    _earlier_conversation(tmp_path / "devices.db", "voice_gone")
    devices = DeviceStore(tmp_path / "devices.db")
    async with Harness(fake, device_store=devices) as client:
        ws, ready = await _connect(client)
        assert ready["type"] == "ready" and fake.store.switched == []
        await ws.close()
        assert devices.reserve("Arya", "Den Echo").session_id is None


async def test_scheduled_reply_is_spoken_on_the_attached_device(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    harness = Harness(fake, device_store=DeviceStore(tmp_path / "devices.db"))
    async with harness as client:
        ws, _ = await _connect(client)
        chat_id = next(iter(fake.store.ids)).rsplit(":", 1)[-1]
        sent = await harness.adapter.send(chat_id, "Your timer is done.", metadata=NOTIFY)
        assert sent.success
        frames, pcm = await _collect_turn(ws)
        assert pcm and frames[-1] == {"type": "turn_done", "turn_id": frames[-1]["turn_id"],
                                      "next_action": "standby"}
        assert any(f["type"] == "transcript" and f["text"] == "Your timer is done." for f in frames)
        await ws.close()
        gone = await harness.adapter.send(chat_id, "Anyone?", metadata=NOTIFY)
        assert not gone.success


async def test_closing_the_socket_stops_the_turn_in_flight(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    fake.hang = True
    async with Harness(fake, device_store=DeviceStore(tmp_path / "devices.db")) as client:
        ws, _ = await _connect(client)
        for chunk in _speech_then_silence_pcm():
            await ws.send_bytes(chunk)
        while not fake.turns:
            await asyncio.sleep(0.01)
        await ws.close()
        for _ in range(500):
            if fake.stopped:
                break
            await asyncio.sleep(0.01)
    assert len(fake.stopped) == 1


async def test_socket_without_names_gets_a_conversation_it_can_reattach(monkeypatch):
    fake = _patch(monkeypatch)
    async with Harness(fake) as client:
        ws, ready = await _connect(client, assistant_name=None, device_name=None)
        conversation = ready["conversation_id"]
        assert ready["type"] == "ready" and conversation.startswith("voice_")
        await _say(ws)
        await ws.close()
        ws, again = await _connect(client, assistant_name=None, device_name=None, conversation_id=conversation)
        assert again["conversation_id"] == conversation
        await _say(ws)
        await ws.close()
    assert [turn["chat_id"] for turn in fake.turns] == [conversation, conversation]


async def test_unknown_conversation_id_is_refused_and_an_earlier_api_one_is_continued(monkeypatch):
    fake = _patch(monkeypatch)
    fake.sessions["voice_old"] = {"id": "voice_old"}
    async with Harness(fake) as client:
        ws, refused = await _connect(client, assistant_name=None, device_name=None, conversation_id="voice_nope")
        assert refused == {"type": "error", "error": "conversation_unavailable"}
        ws, ready = await _connect(client, assistant_name=None, device_name=None, conversation_id="voice_old")
        assert ready["conversation_id"] == "voice_old"
        await ws.close()
    assert [target for _, target in fake.store.switched] == ["voice_old"]


async def test_device_is_refused_when_the_platform_is_not_hosted(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    harness = Harness(fake, device_store=DeviceStore(tmp_path / "devices.db"))
    async with harness as client:
        await harness.adapter.disconnect()
        _, refused = await _connect(client)
        assert refused == {"type": "error", "error": "conversation_unavailable"}
