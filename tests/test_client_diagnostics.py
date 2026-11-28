"""Diagnostic tool calls over the real authenticated route, including Hermes dispatch."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from hermes_converse import client_diagnostics
from hermes_converse.conversation import ConversationHub
from hermes_converse.voice_tools import make_handlers, register_tools
from tests.support.fake_hermes import FakeHermes
from tests.support.plugin_ctx import tool_ctx
from tests.test_command_tool import _frames_until
from tests.test_route import (API_KEY, VOICE_PROTOCOL, Harness, _FakeStreamer, _end, _key_protocol,
                              _patch_speech, _speech_then_silence_pcm, _start_msg)


SNAPSHOT = {"version": "1.2.3", "build": "145", "platform": "macos", "diagnostics": {
    "state": "listening", "muted": False, "conversation_window": "hidden",
    "events": [{"event": "playback_drained", "elapsed_ms": 42}]}}


@pytest.fixture
def device_env(monkeypatch):
    env = {"HERMES_SESSION_PLATFORM": "converse", "HERMES_SESSION_CHAT_ID": ""}
    monkeypatch.setattr("gateway.session_context.get_session_env", lambda key, default="": env.get(key, default))
    return env


@pytest.fixture
async def connected(monkeypatch, device_env):
    streamer = _FakeStreamer([b"\x01\x02"])
    _patch_speech(monkeypatch, streamer)
    harness = Harness(FakeHermes(API_KEY))  # No telemetry configuration.
    async with harness as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        await ws.send_str(_start_msg())
        assert (await ws.receive_json())["type"] == "ready"
        (device_env["HERMES_SESSION_CHAT_ID"],) = harness.route.hub._attachments
        call = make_handlers(harness.route.hub)["client_diagnostics"]
        try:
            yield SimpleNamespace(ws=ws, client=client, call=call, streamer=streamer, hub=harness.route.hub)
        finally:
            await ws.close()


async def _request(connected):
    task = asyncio.create_task(connected.call({}))
    request = await asyncio.wait_for(connected.ws.receive_json(), 2)
    assert request["type"] == "client_diagnostics"
    assert set(request) == {"type", "request_id"}  # Never sends auth or conversation content.
    return task, request


async def _result(task):
    return json.loads(await asyncio.wait_for(task, 2))


class DiagnosticsHermes(FakeHermes):
    def __init__(self):
        super().__init__(API_KEY, deltas=("The client is running version 1.2.3, build 145.",))
        self.hub = None
        self.results = []

    def _call_tool(self):
        from tools.registry import ToolRegistry
        registry = ToolRegistry()
        register_tools(tool_ctx(registry), self.hub)
        return json.loads(registry.dispatch("client_diagnostics", {}))

    async def handle(self, event):
        self.results.append(await asyncio.to_thread(self._call_tool))
        return await super().handle(event)


async def test_agent_dispatch_reads_client_snapshot_without_speaking_raw_data(monkeypatch, device_env):
    fake = DiagnosticsHermes()
    harness = Harness(fake)
    fake.hub = harness.route.hub
    streamer = _FakeStreamer([b"\x01\x02"])
    _patch_speech(monkeypatch, streamer, transcript="Which app version am I running?")
    async with harness as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        await ws.send_str(_start_msg())
        assert (await ws.receive_json())["type"] == "ready"
        (device_env["HERMES_SESSION_CHAT_ID"],) = harness.route.hub._attachments
        for frame in _speech_then_silence_pcm():
            await ws.send_bytes(frame)
        request = (await _frames_until(ws, "client_diagnostics"))[-1]
        await ws.send_json({**request, **SNAPSHOT})
        frames = await _frames_until(ws, "turn_done")
        assert fake.results == [SNAPSHOT]
        assert streamer.requests == ["The client is running version 1.2.3, build 145."]
        assert not any(f["type"] in {"error", "interrupted", "approval_request"} for f in frames)
        assert "client_diagnostics" in fake.turns[0]["instructions"]
        await _end(ws)


async def test_each_call_returns_a_fresh_snapshot(connected):
    first, request = await _request(connected)
    await connected.ws.send_json({**request, **SNAPSHOT})
    assert await _result(first) == SNAPSHOT
    second, next_request = await _request(connected)
    assert next_request["request_id"] != request["request_id"]
    new = {**SNAPSHOT, "version": "1.2.4", "build": "146", "diagnostics": {"muted": True}}
    await connected.ws.send_json({**next_request, **new})
    assert await _result(second) == new


async def test_concurrent_calls_match_responses_in_reverse_order(connected):
    first, request1 = await _request(connected)
    second, request2 = await _request(connected)
    assert request1["request_id"] != request2["request_id"]
    await connected.ws.send_json({**request2, **SNAPSHOT, "build": "second"})
    assert (await _result(second))["build"] == "second"
    assert not first.done()
    await connected.ws.send_json({**request1, **SNAPSHOT, "build": "first"})
    assert (await _result(first))["build"] == "first"


async def test_audio_and_frame_handling_continue_while_diagnostics_wait(connected):
    task, request = await _request(connected)
    await connected.ws.send_bytes(b"\x00\x00" * 480)
    await connected.ws.send_json({"type": "say", "text": "Still speaking."})
    frames = await _frames_until(connected.ws, "turn_done")
    assert any(f["type"] == "speaking" for f in frames)
    assert connected.streamer.requests == ["Still speaking."]
    assert not task.done()
    await connected.ws.send_json({**request, **SNAPSHOT})
    assert await _result(task) == SNAPSHOT


async def test_timeout_and_late_response_do_not_poison_next_call(connected, monkeypatch):
    monkeypatch.setattr(client_diagnostics, "TIMEOUT", .05)
    task, request = await _request(connected)
    assert await _result(task) == {"error": "timeout"}
    monkeypatch.setattr(client_diagnostics, "TIMEOUT", 5)
    next_task, next_request = await _request(connected)
    await connected.ws.send_json({**request, **SNAPSHOT, "build": "stale"})
    await connected.ws.send_json({**next_request, **SNAPSHOT})
    assert await _result(next_task) == SNAPSHOT


async def test_disconnect_settles_all_pending_requests(connected):
    first, _ = await _request(connected)
    second, _ = await _request(connected)
    await connected.ws.close()
    assert await _result(first) == {"error": "disconnected"}
    assert await _result(second) == {"error": "disconnected"}


async def test_replacement_does_not_inherit_a_pending_request(connected):
    task, old_request = await _request(connected)
    replacement = await connected.client.ws_connect(
        "/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
    await replacement.send_str(_start_msg())
    assert (await replacement.receive_json())["type"] == "ready"
    assert await _result(task) == {"error": "disconnected"}
    new_task = asyncio.create_task(connected.call({}))
    new_request = await asyncio.wait_for(replacement.receive_json(), 2)
    assert new_request["type"] == "client_diagnostics"
    assert new_request["request_id"] != old_request["request_id"]
    await replacement.send_json({**old_request, **SNAPSHOT, "build": "stale"})
    await replacement.send_json({**new_request, **SNAPSHOT})
    assert await _result(new_task) == SNAPSHOT
    await _end(replacement)


async def test_other_socket_and_mismatched_ids_cannot_answer(connected):
    task, request = await _request(connected)
    other = await connected.client.ws_connect(
        "/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
    await other.send_str(_start_msg(device_name="Other Echo"))
    assert (await other.receive_json())["type"] == "ready"
    await other.send_json({**request, **SNAPSHOT, "build": "wrong client"})
    await other.send_json({"type": "current_model"})
    assert (await other.receive_json())["type"] == "current_model"
    for request_id in ("unsolicited", [], None):
        await connected.ws.send_json({**request, "request_id": request_id, **SNAPSHOT})
    await connected.ws.send_json({"type": "current_model"})
    assert (await connected.ws.receive_json())["type"] == "current_model"
    assert not task.done()
    await connected.ws.send_json({**request, **SNAPSHOT})
    assert await _result(task) == SNAPSHOT
    await _end(other)


@pytest.mark.parametrize("snapshot", [
    {}, {**SNAPSHOT, "version": 123}, {**SNAPSHOT, "version": ""},
    {**SNAPSHOT, "build": None}, {**SNAPSHOT, "build": " "},
    {**SNAPSHOT, "diagnostics": []}, {**SNAPSHOT, "platform": 12},
    {**SNAPSHOT, "diagnostics": {"latency": float("nan")}},
    {"error": {}}, {"error": " "},
])
async def test_malformed_snapshot_is_not_returned(connected, snapshot):
    task, request = await _request(connected)
    await connected.ws.send_json({**request, **snapshot})
    assert await _result(task) == {"error": "invalid_response"}


async def test_collection_failure_is_returned_as_data(connected):
    task, request = await _request(connected)
    await connected.ws.send_json({**request, "error": "Microphone diagnostics unavailable."})
    assert await _result(task) == {"error": "Microphone diagnostics unavailable."}


@pytest.mark.parametrize("over", [False, True])
async def test_response_limit_counts_utf8_bytes(connected, over):
    task, request = await _request(connected)
    frame = {**request, **SNAPSHOT, "diagnostics": {"event": ""}}
    raw = json.dumps(frame, ensure_ascii=False)
    remaining = client_diagnostics.MAX_BYTES - len(raw.encode("utf-8"))
    frame["diagnostics"]["event"] = "é" * (remaining // 2) + "x" * (remaining % 2 + over)
    raw = json.dumps(frame, ensure_ascii=False)
    assert len(raw.encode("utf-8")) == client_diagnostics.MAX_BYTES + over
    await connected.ws.send_str(raw)
    result = await _result(task)
    assert result == ({"error": "invalid_response"} if over else {
        key: frame[key] for key in ("version", "build", "platform", "diagnostics")})


async def test_only_snapshot_fields_are_forwarded(connected):
    task, request = await _request(connected)
    await connected.ws.send_json({**request, **SNAPSHOT, "unexpected": "not a diagnostic field"})
    assert await _result(task) == SNAPSHOT


async def test_tool_requires_an_attached_voice_client(device_env):
    call = make_handlers(ConversationHub())["client_diagnostics"]
    assert json.loads(await call({})) == {"error": "not_attached"}
    device_env["HERMES_SESSION_PLATFORM"] = "telegram"
    assert json.loads(await call({})) == {"error": "not_a_device"}
