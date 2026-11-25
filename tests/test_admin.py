"""GET /v1/audio/admin end to end: the device list, announcements, and mute and volume."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import WSServerHandshakeError, web

from hermes_converse import admin as admin_module
from hermes_converse.loop import tone_pcm

from tests.support.fake_hermes import FakeHermes
from tests.test_route import (API_KEY, VOICE_PROTOCOL, Harness, _collect_turn, _end, _FakeStreamer,
                              _key_protocol, _patch_speech, _recv, _start_msg)

AUTH = {"Authorization": f"Bearer {API_KEY}"}


async def _device(client, name):
    ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
    await ws.send_str(_start_msg(device_name=name))
    assert json.loads((await _recv(ws)).data)["type"] == "ready"
    return ws


async def _admin(client):
    ws = await client.ws_connect("/v1/audio/admin", headers=AUTH)
    return ws, (await ws.receive_json(timeout=5))["devices"]


async def _json(ws):
    return await ws.receive_json(timeout=5)


def _entry(name, attached=True, muted=None, volume=None, address="127.0.0.1"):
    return {"device_name": name, "attached": attached, "muted": muted, "volume": volume, "address": address}


@pytest.mark.asyncio
async def test_a_wrong_key_gets_401_and_no_socket():
    async with Harness(FakeHermes(API_KEY)) as client:
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            with pytest.raises(WSServerHandshakeError) as refused:
                await client.ws_connect("/v1/audio/admin", headers=headers)
            assert refused.value.status == 401


@pytest.mark.asyncio
async def test_devices_are_listed_and_every_change_reaches_every_admin(monkeypatch):
    _patch_speech(monkeypatch, _FakeStreamer([b"\x01\x02"]))
    async with Harness(FakeHermes(API_KEY)) as client:
        kitchen = await _device(client, "Kitchen")
        first, devices = await _admin(client)
        second, _ = await _admin(client)
        assert devices == [_entry("Kitchen")]

        den = await _device(client, "Den")  # a first connection
        for ws in (first, second):
            assert await _json(ws) == {"type": "device", **_entry("Den")}

        await kitchen.send_json({"type": "mute", "on": True})
        await kitchen.send_json({"type": "volume", "level": 0.4})
        await kitchen.send_json({"type": "volume", "level": 4})  # out of range: ignored
        await kitchen.send_json({"type": "volume", "level": 0.4})  # unchanged: no frame
        for ws in (first, second):
            assert await _json(ws) == {"type": "device", **_entry("Kitchen", muted=True)}
            assert await _json(ws) == {"type": "device", **_entry("Kitchen", muted=True, volume=0.4)}

        await _end(kitchen)
        for ws in (first, second):
            assert await _json(ws) == {"type": "device", **_entry("Kitchen", attached=False)}
        # A later admin still lists the device that left, with nothing reported.
        third, devices = await _admin(client)
        assert devices == [_entry("Den"), _entry("Kitchen", attached=False)]
        await _end(den)


@pytest.mark.asyncio
async def test_announce_speaks_after_the_tone_on_the_named_devices_only(monkeypatch):
    fake = FakeHermes(API_KEY)
    streamer = _FakeStreamer([b"\x01\x02"])
    _patch_speech(monkeypatch, streamer)
    async with Harness(fake) as client:
        kitchen, den = await _device(client, "Kitchen"), await _device(client, "Den")
        admin, _ = await _admin(client)
        await admin.send_json({"type": "announce", "request_id": "7", "message": " The laundry is done. ",
                               "target": ["Kitchen", "Attic"]})
        assert await _json(admin) == {"type": "result", "request_id": "7",
                                      "announced": ["Kitchen"], "skipped": ["Attic"]}
        frames, pcm = await _collect_turn(kitchen)
        assert [f["type"] for f in frames if f["type"] not in ("thinking", "state")] == ["speaking", "turn_done"]
        assert b"".join(pcm) == tone_pcm("announcement", 24000) + b"\x01\x02"  # the tone, then the speech
        assert streamer.requests == ["The laundry is done."]
        assert fake.turns == []  # no agent turn

        # No target is every attached device; one name is a target too.
        await admin.send_json({"type": "announce", "request_id": "8", "message": "Dinner."})
        assert await _json(admin) == {"type": "result", "request_id": "8",
                                      "announced": ["Den", "Kitchen"], "skipped": []}
        for ws in (kitchen, den):
            await _collect_turn(ws)
        await admin.send_json({"type": "announce", "request_id": "9", "message": "Dinner.", "target": " Den "})
        assert (await _json(admin))["announced"] == ["Den"]
        await _collect_turn(den)
        with pytest.raises(asyncio.TimeoutError):  # nothing on the device not named
            await _recv(kitchen, 0.3)
        await _end(kitchen)
        await _end(den)


@pytest.mark.asyncio
async def test_bad_requests_get_bad_request_and_a_frame_with_no_request_id_gets_nothing():
    async with Harness(FakeHermes(API_KEY)) as client:
        admin, devices = await _admin(client)
        assert devices == []
        await admin.send_json({"type": "announce", "message": "no id"})
        await admin.send_str("not json")
        for n, frame in enumerate((
                {"type": "restart"}, {"type": "announce", "message": "  "},
                {"type": "announce", "message": "hi", "target": [1]},
                {"type": "set", "device_name": "Kitchen"},
                {"type": "set", "device_name": "Kitchen", "volume": 1.01},
                {"type": "set", "device_name": "Kitchen", "volume": True},
                {"type": "set", "device_name": "Kitchen", "muted": "yes"})):
            await admin.send_json({**frame, "request_id": str(n)})
            assert await _json(admin) == {"type": "result", "request_id": str(n), "error": "bad_request"}
        await admin.send_json({"type": "set", "request_id": "x", "device_name": "Kitchen", "muted": True})
        assert await _json(admin) == {"type": "result", "request_id": "x", "error": "unknown_device"}


@pytest.mark.asyncio
async def test_set_sends_the_frames_and_answers_with_the_devices_report(monkeypatch):
    _patch_speech(monkeypatch, _FakeStreamer([b"\x01\x02"]))
    monkeypatch.setattr(admin_module, "SET_TIMEOUT", 0.3)
    async with Harness(FakeHermes(API_KEY)) as client:
        kitchen = await _device(client, "Kitchen")
        admin, _ = await _admin(client)
        await admin.send_json({"type": "set", "request_id": "1", "device_name": "Kitchen",
                               "muted": True, "volume": 0.4})
        assert json.loads((await _recv(kitchen)).data) == {"type": "mute", "on": True}
        assert json.loads((await _recv(kitchen)).data) == {"type": "volume", "level": 0.4}
        await kitchen.send_json({"type": "mute", "on": True})
        await kitchen.send_json({"type": "volume", "level": 0.35})  # the device decides
        frames = [await _json(admin) for _ in range(3)]
        assert {"type": "result", "request_id": "1", "device": _entry("Kitchen", muted=True, volume=0.35)} in frames
        assert {"type": "device", **_entry("Kitchen", muted=True, volume=0.35)} in frames

        # A device that does not answer is answered with its last report.
        await admin.send_json({"type": "set", "request_id": "2", "device_name": "Kitchen", "muted": False})
        assert await _json(admin) == {"type": "result", "request_id": "2",
                                      "device": _entry("Kitchen", muted=True, volume=0.35)}

        await _end(kitchen)
        assert (await _json(admin))["attached"] is False
        await admin.send_json({"type": "set", "request_id": "3", "device_name": "Kitchen", "volume": 0.1})
        assert await _json(admin) == {"type": "result", "request_id": "3", "error": "not_attached"}


@pytest.mark.asyncio
async def test_the_announce_tool_speaks_on_every_device_but_the_callers(monkeypatch):
    from gateway import session_context
    from hermes_converse.voice_tools import make_handlers

    streamer = _FakeStreamer([b"\x01\x02"])
    _patch_speech(monkeypatch, streamer)
    async with Harness(FakeHermes(API_KEY)) as client:
        kitchen, den = await _device(client, "Kitchen"), await _device(client, "Den")
        hub = client.app["hub"]
        caller = next(key for key, item in hub._attachments.items() if item.session.device_name == "Kitchen")
        monkeypatch.setattr(session_context, "get_session_env", lambda key, default="": {
            "HERMES_SESSION_PLATFORM": "converse", "HERMES_SESSION_CHAT_ID": caller}.get(key, default))
        announce = make_handlers(hub)["announce"]
        assert "error" in json.loads(announce({"message": " "}))
        assert json.loads(announce({"message": "Dinner's ready."})) == {"result": "ok", "devices": 1}
        _, pcm = await _collect_turn(den)
        assert b"".join(pcm) == tone_pcm("announcement", 24000) + b"\x01\x02"
        assert streamer.requests == ["Dinner's ready."]
        with pytest.raises(asyncio.TimeoutError):
            await _recv(kitchen, 0.3)
        await _end(kitchen)
        await _end(den)


@pytest.mark.asyncio
async def test_the_address_is_the_forwarded_one_and_is_kept_while_the_device_is_away(monkeypatch):
    _patch_speech(monkeypatch, _FakeStreamer([b"\x01\x02"]))
    harness = Harness(FakeHermes(API_KEY))
    async with harness as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()),
                                     headers={"X-Forwarded-For": "192.168.1.52, 10.0.0.1"})
        await ws.send_str(_start_msg(device_name="Kitchen"))
        assert json.loads((await _recv(ws)).data)["type"] == "ready"
        admin, devices = await _admin(client)
        assert devices == [_entry("Kitchen", address="192.168.1.52")]
        await _end(ws)
        assert await _json(admin) == {"type": "device", **_entry("Kitchen", attached=False, address="192.168.1.52")}
        # It is stored, not only remembered: a server that restarted still knows it.
        await asyncio.gather(*harness.route.admin._tasks)
        harness.route.admin._seen.clear()
        _, devices = await _admin(client)
        assert devices == [_entry("Kitchen", attached=False, address="192.168.1.52")]
        stored = harness.route._devices.settings()["Kitchen"]
        assert stored["address"] == "192.168.1.52" and stored["last_attached"] > 0


def test_a_forwarded_address_that_is_not_an_ip_falls_back_to_the_peer():
    from types import SimpleNamespace

    request = SimpleNamespace(headers={"X-Forwarded-For": "<script>"}, remote="10.0.0.7")
    assert admin_module.client_address(request) == "10.0.0.7"
    assert admin_module.client_address(SimpleNamespace(headers={}, remote=None)) is None


@pytest.mark.asyncio
async def test_a_device_unattached_for_30_days_is_unlisted_and_removed_with_its_conversations(monkeypatch):
    import time

    _patch_speech(monkeypatch, _FakeStreamer([b"\x01\x02"]))
    harness = Harness(FakeHermes(API_KEY))
    async with harness as client:
        store, admin = harness.route._devices, harness.route.admin
        kitchen = await _device(client, "Kitchen")
        for name in ("Old", "Attic"):
            await _end(await _device(client, name))
        await asyncio.sleep(0.2)
        await asyncio.gather(*admin._tasks)
        long_ago = time.time() - 31 * 86400
        store.update_settings("Old", last_attached=long_ago)
        store.update_settings("Kitchen", last_attached=long_ago)  # attached: never removed
        with store._lock:  # a row from before last_attached was recorded
            store._connection().execute("INSERT INTO devices (device_name, recipient_id) VALUES ('Ancient', 'r')")
            store._connection().commit()
        admin._seen.clear()  # as after a restart

        ws, devices = await _admin(client)
        assert [d["device_name"] for d in devices] == ["Attic", "Kitchen"]
        await ws.send_json({"type": "set", "request_id": "1", "device_name": "Old", "muted": True})
        assert (await _json(ws))["error"] == "unknown_device"
        await ws.send_json({"type": "set", "request_id": "2", "device_name": "Attic", "muted": True})
        assert (await _json(ws))["error"] == "not_attached"

        await admin.purge()
        assert [d["device_name"] for d in (await _json(ws))["devices"]] == ["Attic", "Kitchen"]
        assert sorted(store.settings()) == ["Ancient", "Attic", "Kitchen"]
        with store._lock:
            titles = [row[0] for row in store._connection().execute("SELECT title FROM conversations")]
        assert sorted(titles) == ["Arya: Attic", "Arya: Kitchen"]
        await _end(kitchen)
