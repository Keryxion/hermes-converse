"""The ``command`` voice tool (specs/server.md, "`command`") end to end: the agent's turn calls the
tool, the device is asked as for an approval, and the command runs in the turn, or for a new
conversation once the turn's reply has played. The harness's ``approval_choice`` is ``deny``: the tool
asks anyway."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web

from hermes_converse import command_catalog
from hermes_converse.approvals import phrase
from hermes_converse.voice_tools import make_handlers
from tests.support.fake_hermes import FakeHermes
from tests.test_route import (API_KEY, VOICE_PROTOCOL, Harness, _FakeStreamer, _key_protocol, _patch_speech,
                              _recv, _speech_then_silence_pcm, _start_msg)


class ToolHermes(FakeHermes):
    """The voice turn calls the ``command`` tool first, from a worker thread as the agent does."""

    def __init__(self, tool_text: str) -> None:
        super().__init__(API_KEY, deltas=("Done.",))
        self.tool_text = tool_text
        self.results: list[str] = []
        self.hub = None
        self.command_replies = {"compress": "Compressed.", "reset": "New session started."}

    def _call_tool(self) -> str:
        handler = make_handlers(self.hub)["command"]
        return json.loads(asyncio.run(handler({"text": self.tool_text})))["result"]

    async def handle(self, event):
        if not event.get_command() and not self.results:
            self.results.append(await asyncio.to_thread(self._call_tool))
        return await super().handle(event)


@pytest.fixture
def device_env(monkeypatch):
    """The tool call comes from the device's own conversation."""
    env = {"HERMES_SESSION_PLATFORM": "converse", "HERMES_SESSION_CHAT_ID": ""}
    monkeypatch.setattr("gateway.session_context.get_session_env", lambda key, default="": env.get(key, default))
    return env


async def _frames_until(ws, kind, timeout=8.0):
    frames = []
    while True:
        msg = await _recv(ws, timeout)
        if msg.type == web.WSMsgType.BINARY:
            continue
        assert msg.type == web.WSMsgType.TEXT, msg
        frames.append(json.loads(msg.data))
        if frames[-1]["type"] == kind:
            return frames


async def _turn_calling(monkeypatch, device_env, tool_text, choice, *, transcript="compress this"):
    """Connect, speak, and answer the tool's approval_request with *choice* (None: never). Returns the
    fake, the harness, the socket, the approval_request (None when there was none), the frames up to
    turn_done and the TTS stand-in."""
    fake = ToolHermes(tool_text)
    streamer = _FakeStreamer([b"\x01\x02"])
    _patch_speech(monkeypatch, streamer, transcript=transcript)
    harness = Harness(fake)
    fake.hub = harness.route.hub
    client = await harness.__aenter__()
    ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
    await ws.send_str(_start_msg())
    assert (await ws.receive_json())["type"] == "ready"
    (device_env["HERMES_SESSION_CHAT_ID"],) = harness.route.hub._attachments
    for frame in _speech_then_silence_pcm():
        await ws.send_bytes(frame)
    frames = []
    if choice is not None:
        frames = await _frames_until(ws, "approval_request")
        await ws.send_json({"type": "approval_response", "approval_id": frames[-1]["approval_id"],
                            "choice": choice})
    frames += await _frames_until(ws, "turn_done", timeout=12.0)
    request = next((f for f in frames if f["type"] == "approval_request"), None)
    return fake, harness, ws, request, frames, streamer


async def _close(harness, ws):
    if not ws.closed:
        await ws.send_str(json.dumps({"stop": True}))
        await ws.close()
    await harness.__aexit__(None, None, None)


async def test_an_approved_command_runs_in_the_turn_and_returns_its_reply(monkeypatch, device_env):
    fake, harness, ws, request, frames, streamer = await _turn_calling(
        monkeypatch, device_env, "/compress here 3", "once")
    try:
        description = phrase(command_catalog.description("compress"))
        summary = f"Arya wants to run a command for you: {description}, here 3. Okay?"
        assert request["summary"] == summary and request["command"] == "/compress here 3"
        assert request["choices"] == ["once", "deny"]
        assert {"type": "tone", "tone": "approval"} in frames
        assert " ".join(streamer.requests).startswith(summary + " Say yes or no.")  # the question is spoken
        assert {"type": "approval_resolved", "approval_id": request["approval_id"], "outcome": "approved"} in frames
        assert fake.results == ["Compressed."]  # the reply is the tool result, not speech
        assert "Compressed." not in " ".join(streamer.requests)
        assert fake.commands == ["/compress here 3"]
        assert not ws.closed
    finally:
        await _close(harness, ws)


async def test_a_denied_command_runs_nothing(monkeypatch, device_env):
    fake, harness, ws, request, frames, _ = await _turn_calling(monkeypatch, device_env, "/compress", "deny")
    try:
        assert fake.results == ["denied"] and fake.commands == []
        assert {"type": "approval_resolved", "approval_id": request["approval_id"], "outcome": "denied"} in frames
    finally:
        await _close(harness, ws)


async def test_no_answer_before_the_timeout_runs_nothing(monkeypatch, device_env):
    monkeypatch.setattr("gateway.platforms.base_exec_approval.approval_timeout_seconds", lambda: 1)
    fake, harness, ws, request, frames, _ = await _turn_calling(monkeypatch, device_env, "/compress", None)
    try:
        assert request is not None
        assert fake.results == ["denied"] and fake.commands == []
        assert {"type": "approval_resolved", "approval_id": request["approval_id"], "outcome": "expired"} in frames
    finally:
        await _close(harness, ws)


async def test_an_unknown_command_is_not_asked(monkeypatch, device_env):
    fake, harness, ws, request, _frames, _ = await _turn_calling(monkeypatch, device_env, "/no-such-command", None)
    try:
        assert request is None and fake.results == ["unknown_command"] and fake.commands == []
    finally:
        await _close(harness, ws)


async def test_new_starts_the_conversation_only_after_the_reply_has_played(monkeypatch, device_env):
    fake, harness, ws, request, frames, _ = await _turn_calling(
        monkeypatch, device_env, "/reset", "once", transcript="start over")
    try:
        description = phrase(command_catalog.description("new"))
        assert request["summary"] == f"Arya wants to run a command for you: {description}. Okay?"
        assert "new conversation" in fake.results[0]
        turn_done = frames[-1]
        # The reply is still playing: nothing has run and the socket is open.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws.receive(), 0.5)
        assert fake.commands == [] and not ws.closed
        await ws.send_json({"type": "state", "turn_id": turn_done["turn_id"], "state": "listening"})
        msg = await _recv(ws)
        assert msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSED) and ws.close_code == 4410
        assert fake.commands == ["/reset"]
    finally:
        await _close(harness, ws)


def test_the_tool_needs_an_attached_voice_device(monkeypatch, device_env):
    from hermes_converse.conversation import ConversationHub

    command = make_handlers(ConversationHub())["command"]
    device_env["HERMES_SESSION_CHAT_ID"] = "den"
    assert json.loads(asyncio.run(command({"text": "/new"}))) == {"result": "not_attached"}
    device_env["HERMES_SESSION_PLATFORM"] = "telegram"
    assert json.loads(asyncio.run(command({"text": "/new"}))) == {"result": "not_a_device"}
