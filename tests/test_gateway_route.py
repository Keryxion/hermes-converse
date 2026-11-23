"""A socket end to end with its voice turns running in a gateway conversation."""

from __future__ import annotations

import asyncio

import pytest

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
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "CONVERSE.md"
    path.write_text("Before reconnect.", encoding="utf-8")
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
        path.write_text("After reconnect.", encoding="utf-8")
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
        (resumed,) = fake.turns
        assert "Before reconnect." in resumed["instructions"]
        await _say(ws)
        assert "After reconnect." in fake.turns[1]["instructions"]
        assert "Before reconnect." not in fake.turns[1]["instructions"]
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


@pytest.mark.parametrize("fresh_piece", [False, True])
async def test_reconnect_during_paced_reply_only_speaks_fresh_text(tmp_path, monkeypatch, fresh_piece):
    """Real sockets replace a driver while it holds an old PCM chunk for pacing."""
    from aiohttp import web
    from hermes_converse import loop, speech

    fake = _patch(monkeypatch)
    fake.hang, fake.after_hang = True, ("Fresh.",) if fresh_piece else ()
    old_first = b"\x11\x11" * 48000  # two seconds forces the next chunk to wait
    old_pending, fresh = b"\x22\x22" * 240, b"\x33\x33" * 240

    class Streamer:
        sample_rate = 24000

        def stream(self, text):
            yield from [fresh] if "Fresh" in text else [old_first, old_pending]

    monkeypatch.setattr(speech, "streaming_provider", lambda cfg: Streamer())
    monkeypatch.setattr(loop, "MAX_AUDIO_LEAD_SECONDS", 0)
    harness = Harness(fake, device_store=DeviceStore(tmp_path / "devices.db"))
    async with harness as client:
        ws, first = await _connect(client)
        for chunk in _speech_then_silence_pcm():
            await ws.send_bytes(chunk)
        while True:
            message = await ws.receive(timeout=3)
            if message.type == web.WSMsgType.BINARY:
                assert message.data == old_first
                break
        # The first PCM has filled the playback lead; the next old chunk is paced.
        await asyncio.sleep(0.05)
        replacement, ready = await _connect(client)
        assert ready["recipient_id"] == first["recipient_id"]
        replay = []
        while not replay or replay[-1]["type"] != "thinking":
            replay.append(await replacement.receive_json(timeout=3))
        assert any(f.get("replay") and f.get("text") == "Hello." for f in replay)
        assert any(f.get("state") == "thinking" for f in replay)
        fake.hang = False
        frames, pcm = await _collect_turn(replacement)
        assert pcm == ([fresh] if fresh_piece else [])
        fresh_index = len(frames)
        if fresh_piece:
            fresh_index = next(i for i, f in enumerate(frames)
                               if f["type"] == "transcript" and f.get("text", "").endswith("Fresh."))
        assert not any(f["type"] == "speaking" or f.get("state") == "speaking"
                       for f in frames[:fresh_index])
        assert sum(f["type"] == "speaking" for f in frames) == fresh_piece
        assert frames[-1]["type"] == "turn_done"
        assert len(fake.turns) == 1 and fake.turns[0]["status"] == "completed"
        await replacement.close()
        await ws.close()
