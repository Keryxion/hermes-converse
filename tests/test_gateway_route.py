"""A socket end to end with its voice turns running in a gateway conversation."""

from __future__ import annotations

import asyncio

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


async def test_voice_turn_runs_in_the_gateway_conversation(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    async with Harness(fake, device_store=DeviceStore(tmp_path / "devices.db")) as client:
        ws, ready = await _connect(client)
        assert ready["type"] == "ready" and ready["device_name"] == "Den Echo" and ready["recipient_id"]
        frames, pcm = await _say(ws)
        kinds = [f["type"] for f in frames]
        assert kinds[:3] == ["state", "thinking", "transcript"] and "speaking" in kinds and kinds[-1] == "turn_done"
        assert pcm
        spoken = next(f for f in frames if f["type"] == "transcript" and f["role"] == "assistant")
        assert spoken["text"] == "Hello."
        await ws.close()
    (turn,) = fake.turns
    assert turn["text"] == "hello" and turn["status"] == "completed"
    assert turn["chat_name"] == "Arya: Den Echo" and turn["user_name"] == "Den Echo"
    assert turn["user_id"] == ready["recipient_id"]
    assert "Your name is Arya" in turn["instructions"] and '"device_name": "Den Echo"' in turn["instructions"]


async def test_typed_text_runs_in_gateway_without_microphone(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    async with Harness(fake, device_store=DeviceStore(tmp_path / "devices.db")) as client:
        ws, ready = await _connect(client)
        await ws.send_json({"type": "text", "text": "  Send diagnostic report  ", "request_id": "draft-1"})
        assert await ws.receive_json(timeout=3) == {
            "type": "text_ack", "request_id": "draft-1", "accepted": True}
        frames, pcm = await _collect_turn(ws)
        assert pcm
        assert any(f["type"] == "transcript" and f["role"] == "user"
                   and f["text"] == "Send diagnostic report" for f in frames)
        assert frames[-1]["type"] == "turn_done"
        await ws.close()
    assert len(fake.turns) == 1
    assert fake.turns[0]["text"] == "Send diagnostic report"
    assert fake.turns[0]["user_id"] == ready["recipient_id"]


async def test_typed_text_rejects_invalid_payloads_without_turn(monkeypatch):
    fake = _patch(monkeypatch)
    async with Harness(fake) as client:
        ws, _ = await _connect(client)
        for request_id, text, error in [
            ("", "hello", "bad_request_id"),
            ("a space", "hello", "bad_request_id"),
            ("id-1", "  ", "bad_text"),
            ("id-2", 5, "bad_text"),
            ("id-3", "🙂" * 16385, "bad_text"),
            ("x" * 81, "hello", "bad_request_id"),
        ]:
            await ws.send_json({"type": "text", "request_id": request_id, "text": text})
            assert await ws.receive_json(timeout=3) == {
                "type": "text_ack", "request_id": request_id if error == "bad_text" else "",
                "accepted": False, "error": error}
        await ws.close()
    assert fake.turns == []


async def test_typed_text_accepts_64kb_utf8_boundary(monkeypatch):
    fake = _patch(monkeypatch)
    async with Harness(fake) as client:
        ws, _ = await _connect(client)
        text = "🙂" * 16384
        await ws.send_json({"type": "text", "text": text, "request_id": "boundary"})
        assert await ws.receive_json(timeout=3) == {
            "type": "text_ack", "request_id": "boundary", "accepted": True}
        await ws.send_json({"type": "text", "text": "again", "request_id": "boundary"})
        # A turn may emit frames between independent acknowledgements.
        while True:
            frame = await ws.receive_json(timeout=3)
            if frame["type"] == "text_ack":
                break
        assert frame == {"type": "text_ack", "request_id": "boundary", "accepted": False,
                         "error": "duplicate_request_id"}
        # The gateway turn may still be running; the acknowledgement is queue admission only.
        await ws.close()


async def test_reconnect_keeps_the_same_gateway_conversation(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    async with Harness(fake, device_store=DeviceStore(tmp_path / "devices.db")) as client:
        ws, first = await _connect(client)
        await _say(ws)
        await ws.close()
        ws, second = await _connect(client)
        assert second["recipient_id"] == first["recipient_id"]
        await ws.close()
    assert len(fake.store.ids) == 1


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


async def test_closing_the_socket_keeps_the_turn_in_the_gateway(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    fake.hang = True
    harness = Harness(fake, device_store=DeviceStore(tmp_path / "devices.db"))
    async with harness as client:
        ws, _ = await _connect(client)
        for chunk in _speech_then_silence_pcm():
            await ws.send_bytes(chunk)
        while not fake.turns:
            await asyncio.sleep(0.01)
        await ws.close()
        for _ in range(100):
            if harness.route.hub.detached:
                break
            await asyncio.sleep(0.01)
        assert harness.route.hub.detached and not fake.stopped
        fake.hang = False
        for _ in range(100):
            if fake.turns[0]["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        assert fake.turns[0]["status"] == "completed"


async def test_a_socket_cannot_name_its_own_conversation(tmp_path, monkeypatch):
    """There is no client-chosen conversation: the names alone select it."""
    fake = _patch(monkeypatch)
    async with Harness(fake, device_store=DeviceStore(tmp_path / "devices.db")) as client:
        _, refused = await _connect(client, conversation_id="voice_nope")
        assert refused == {"type": "error", "error": "bad_start"}
    assert not fake.store.ids


async def test_device_is_refused_when_the_platform_is_not_hosted(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    harness = Harness(fake, device_store=DeviceStore(tmp_path / "devices.db"))
    async with harness as client:
        await harness.adapter.disconnect()
        _, refused = await _connect(client)
        assert refused == {"type": "error", "error": "conversation_unavailable"}


async def test_reconnecting_during_a_turn_is_told_where_it_stands(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    fake.hang, fake.tool_calls = True, 2
    harness = Harness(fake, device_store=DeviceStore(tmp_path / "devices.db"))
    async with harness as client:
        ws, _ = await _connect(client)
        for chunk in _speech_then_silence_pcm():
            await ws.send_bytes(chunk)
        live = []
        while not any(f["type"] == "transcript" and f["role"] == "assistant" for f in live):
            live.append(await ws.receive_json(timeout=3))
        turn_id = live[0]["turn_id"]
        user = next(f for f in live if f["type"] == "transcript" and f["role"] == "user")
        await ws.close()
        for _ in range(100):
            if harness.route.hub.detached:
                break
            await asyncio.sleep(0.01)
        ws, ready = await _connect(client)
        assert ready["type"] == "ready"
        # The replay names the turn as live did, and holds its user message only.
        replayed = await ws.receive_json(timeout=3)
        assert replayed == {**user, "replay": True}
        so_far = await ws.receive_json(timeout=3)
        assert so_far == {"type": "transcript", "role": "assistant", "turn_id": turn_id,
                          "text": "Hello.", "replay": True}
        state = await ws.receive_json(timeout=3)
        assert (state["type"], state["state"], state["turn_id"]) == ("state", "thinking", turn_id)
        thinking = await ws.receive_json(timeout=3)
        assert (thinking["type"], thinking["turn_id"], thinking["tool_calls"]) == ("thinking", turn_id, 2)
        fake.hang = False
        rest, _ = await _collect_turn(ws)
        closing = next(f for f in rest if f["type"] == "transcript")
        assert closing["text"] == "" and closing["message_id"] and closing["turn_id"] == turn_id
        assert rest[-1] == {"type": "turn_done", "turn_id": turn_id}
        await ws.close()


async def test_opus_the_server_cannot_set_up_is_audio_unavailable(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)

    def missing():
        raise OSError("Opus audio requires the system libopus library")
    monkeypatch.setattr("hermes_converse.opus._library", missing)
    async with Harness(fake, device_store=DeviceStore(tmp_path / "devices.db")) as client:
        ws, refused = await _connect(client, audio_format="opus")
        assert refused == {"type": "error", "error": "audio_unavailable"}
        assert (await ws.receive(timeout=3)).data == 4503
        _, ready = await _connect(client)  # PCM16 is unaffected
        assert ready["type"] == "ready" and ready["output"]["format"] == "pcm16"


async def test_say_sends_no_transcript(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    async with Harness(fake, device_store=DeviceStore(tmp_path / "devices.db")) as client:
        ws, _ = await _connect(client)
        await ws.send_json({"type": "say", "text": "Say hey Arya to talk to me."})
        frames, pcm = await _collect_turn(ws)
        assert pcm and [f["type"] for f in frames if f["type"] == "transcript"] == []
        assert frames[-1]["next_action"] == "standby"
        await ws.close()
