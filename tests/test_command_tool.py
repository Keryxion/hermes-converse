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
from hermes_converse.voice_tools import make_handlers, register_tools
from tests.support.fake_hermes import FakeHermes
from tests.test_route import (API_KEY, VOICE_PROTOCOL, Harness, _FakeStreamer, _key_protocol, _patch_speech,
                              _recv, _speech_then_silence_pcm, _start_msg)


class ToolHermes(FakeHermes):
    """The voice turn calls the ``command`` tool first, from a worker thread as the agent does."""

    def __init__(self, tool_name: str, args=None) -> None:
        super().__init__(API_KEY, deltas=("Done.",))
        self.tool_args = {"name": tool_name, "args": [] if args is None else args}
        self.results: list[str] = []
        self.hub = None
        self.reply_gate = None
        self.command_replies = {"compress": "Compressed.", "new": "New session started.", "reset": "New session started."}

    def _call_tool(self) -> str:
        from tools.registry import ToolRegistry
        from types import SimpleNamespace

        registry = ToolRegistry()
        register_tools(SimpleNamespace(register_tool=registry.register), self.hub)
        return json.loads(registry.dispatch("command", self.tool_args))["result"]

    async def handle(self, event):
        if not event.get_command() and not self.results:
            self.results.append(await asyncio.to_thread(self._call_tool))
            if self.reply_gate is not None:
                await self.reply_gate.wait()
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


async def _turn_calling(monkeypatch, device_env, tool_name, choice, *, args=None, transcript="compress this",
                        settings=None):
    """Connect, speak, and answer the tool's approval_request with *choice* (None: never). Returns the
    fake, the harness, the socket, the approval_request (None when there was none), the frames up to
    turn_done and the TTS stand-in."""
    fake = ToolHermes(tool_name, args)
    streamer = _FakeStreamer([b"\x01\x02"])
    _patch_speech(monkeypatch, streamer, transcript=transcript)
    harness = Harness(fake, settings)
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
        if choice == "spoken":
            attachment = fake.hub.attachment(device_env["HERMES_SESSION_CHAT_ID"])
            assert attachment.session.answer_hook("yes")
        elif choice == "cancel":
            await ws.send_json({"type": "cancel", "turn_id": frames[-1]["turn_id"]})
        else:
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
        monkeypatch, device_env, "compress", "once", args=["here", "3"])
    try:
        description = command_catalog.description("compress")
        summary = f"Arya wants permission for: {description}, here, 3. Okay?"
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
    fake, harness, ws, request, frames, _ = await _turn_calling(monkeypatch, device_env, "compress", "deny")
    try:
        assert fake.results == ["denied"] and fake.commands == []
        assert {"type": "approval_resolved", "approval_id": request["approval_id"], "outcome": "denied"} in frames
    finally:
        await _close(harness, ws)


async def test_cancelling_the_approval_runs_nothing(monkeypatch, device_env):
    fake, harness, ws, request, frames, _ = await _turn_calling(monkeypatch, device_env, "compress", "cancel")
    try:
        assert fake.commands == []
        assert not harness.adapter.approvals._pending
    finally:
        await _close(harness, ws)


async def test_a_new_user_request_can_approve_new_after_a_denial(monkeypatch, device_env):
    fake, harness, ws, request, frames, _ = await _turn_calling(
        monkeypatch, device_env, "new", "deny")
    try:
        assert fake.results == ["denied"] and fake.commands == []
        await ws.send_json({"type": "state", "turn_id": frames[-1]["turn_id"], "state": "listening"})
        monkeypatch.setattr("hermes_converse.speech.transcribe_wav", lambda *_a, **_kw: "start a new session now")
        fake.results.clear()
        for pcm in _speech_then_silence_pcm():
            await ws.send_bytes(pcm)
        frames = await _frames_until(ws, "approval_request")
        assert frames[-1]["approval_id"] != request["approval_id"]
        await ws.send_json({"type": "approval_response", "approval_id": frames[-1]["approval_id"], "choice": "once"})
        frames = await _frames_until(ws, "turn_done")
        assert "new conversation" in fake.results[0] and fake.commands == []
        await ws.send_json({"type": "state", "turn_id": frames[-1]["turn_id"], "state": "listening"})
        msg = await _recv(ws)
        assert msg.type == web.WSMsgType.CLOSE and ws.close_code == 4410
        assert fake.commands == ["/new"]
    finally:
        await _close(harness, ws)


async def test_no_answer_before_the_timeout_runs_nothing(monkeypatch, device_env):
    monkeypatch.setattr("gateway.platforms.base_exec_approval.approval_timeout_seconds", lambda: 1)
    fake, harness, ws, request, frames, _ = await _turn_calling(monkeypatch, device_env, "compress", None)
    try:
        assert request is not None
        assert fake.results == ["denied"] and fake.commands == []
        assert {"type": "approval_resolved", "approval_id": request["approval_id"], "outcome": "expired"} in frames
    finally:
        await _close(harness, ws)


async def test_an_unknown_command_is_not_asked(monkeypatch, device_env):
    fake, harness, ws, request, _frames, _ = await _turn_calling(monkeypatch, device_env, "no-such-command", None)
    try:
        assert request is None and fake.results == ["unknown_command"] and fake.commands == []
    finally:
        await _close(harness, ws)


@pytest.mark.parametrize("playback", ["state", "timeout"])
async def test_new_starts_the_conversation_only_after_the_reply_has_played(monkeypatch, device_env, playback):
    fake, harness, ws, request, frames, _ = await _turn_calling(
        monkeypatch, device_env, "reset", "once", transcript="start over",
        settings={"playback_tail_timeout_seconds": .1} if playback == "timeout" else None)
    try:
        description = command_catalog.description("new")
        assert request["summary"] == f"Arya wants permission for: {description}. Okay?"
        assert "new conversation" in fake.results[0]
        turn_done = frames[-1]
        if playback == "timeout":
            msg = await _recv(ws)
            assert msg.type == web.WSMsgType.CLOSE and ws.close_code == 4410
            assert fake.commands == ["/reset"]
            return
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
    assert json.loads(asyncio.run(command({"name": "new"}))) == {"result": "not_attached"}
    device_env["HERMES_SESSION_PLATFORM"] = "telegram"
    assert json.loads(asyncio.run(command({"name": "new"}))) == {"result": "not_a_device"}


async def test_spoken_approval_runs_the_command_without_a_screen(monkeypatch, device_env):
    monkeypatch.setattr("hermes_converse.approvals._aux", lambda *_: "once")
    fake, harness, ws, request, frames, _ = await _turn_calling(
        monkeypatch, device_env, "compress", "spoken")
    try:
        assert fake.results == ["Compressed."] and fake.commands == ["/compress"]
    finally:
        await _close(harness, ws)


async def test_pending_command_can_be_answered_after_reconnect(monkeypatch, device_env):
    fake = ToolHermes("compress")
    _patch_speech(monkeypatch, _FakeStreamer([b"\x01\x02"]))
    harness = Harness(fake)
    fake.hub = harness.route.hub
    async with harness as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        await ws.send_str(_start_msg())
        assert (await ws.receive_json())["type"] == "ready"
        (device_env["HERMES_SESSION_CHAT_ID"],) = fake.hub._attachments
        for pcm in _speech_then_silence_pcm():
            await ws.send_bytes(pcm)
        frames = await _frames_until(ws, "approval_request")
        request_id = frames[-1]["approval_id"]
        await ws.close()
        async with asyncio.timeout(2):
            while not fake.hub.detached:
                await asyncio.sleep(.01)
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        try:
            await ws.send_str(_start_msg())
            assert (await ws.receive_json())["type"] == "ready"
            frames = await _frames_until(ws, "approval_request")
            assert frames[-1]["approval_id"] == request_id
            await ws.send_json({"type": "approval_response", "approval_id": request_id, "choice": "once"})
            async with asyncio.timeout(2):
                while not fake.results:
                    await asyncio.sleep(.01)
            assert fake.results == ["Compressed."] and fake.commands == ["/compress"]
        finally:
            await ws.close()


@pytest.mark.parametrize("arguments", [
    {"name": "/new"}, {"name": "new", "args": "x"}, {"name": "new", "args": [3]},
    {"name": "new", "args": None}, {"text": "new"},
])
async def test_malformed_tool_calls_never_reach_the_socket(device_env, arguments):
    from hermes_converse.conversation import ConversationHub
    from types import SimpleNamespace

    hub = ConversationHub()
    device_env["HERMES_SESSION_CHAT_ID"] = "den"
    attachment = hub.attach("den", SimpleNamespace(stopped=False))
    attachment.command = lambda *_: pytest.fail("invalid arguments reached the socket")
    assert json.loads(await make_handlers(hub)["command"](arguments)) == {"result": "bad_command"}


async def test_server_started_new_waits_for_the_final_reply_not_the_approval(monkeypatch, device_env):
    from gateway.platforms.event import MessageEvent

    fake = ToolHermes("reset")
    fake.reply_gate = asyncio.Event()
    streamer = _FakeStreamer([b"\x01\x02"])
    _patch_speech(monkeypatch, streamer)
    harness = Harness(fake)
    fake.hub = harness.route.hub
    client = await harness.__aenter__()
    ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
    try:
        await ws.send_str(_start_msg())
        ready = await ws.receive_json()
        (chat_id,) = fake.hub._attachments
        device_env["HERMES_SESSION_CHAT_ID"] = chat_id
        source = harness.adapter.source_for(chat_id=chat_id, title="Reminder",
                                            recipient_id=ready["recipient_id"], device_name="Test")
        await harness.adapter.handle_message(MessageEvent(text="Reminder", source=source,
                                                         allow_gateway_control=False))
        frames = await _frames_until(ws, "approval_request")
        await ws.send_json({"type": "approval_response", "approval_id": frames[-1]["approval_id"], "choice": "once"})
        frames = await _frames_until(ws, "turn_done")
        await ws.send_json({"type": "state", "turn_id": frames[-1]["turn_id"], "state": "listening"})
        # The approval outcome is spoken separately from the question and the final reply.
        frames = await _frames_until(ws, "turn_done")
        await ws.send_json({"type": "state", "turn_id": frames[-1]["turn_id"], "state": "listening"})
        assert "Approved." in streamer.requests
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws.receive(), .15)
        assert fake.commands == [] and not ws.closed
        assert "new conversation" in fake.results[0]
        fake.reply_gate.set()
        frames = await _frames_until(ws, "turn_done")
        assert fake.commands == []
        await ws.send_json({"type": "state", "turn_id": frames[-1]["turn_id"], "state": "listening"})
        msg = await _recv(ws)
        assert msg.type == web.WSMsgType.CLOSE and ws.close_code == 4410
        assert fake.commands == ["/reset"]
    finally:
        fake.reply_gate.set()
        await ws.close()
        await harness.__aexit__(None, None, None)


@pytest.mark.parametrize("end", ["disconnect", "cancel"])
async def test_new_is_dropped_if_the_final_reply_is_not_played(monkeypatch, device_env, end):
    fake, harness, ws, request, frames, _ = await _turn_calling(
        monkeypatch, device_env, "reset", "once")
    try:
        if end == "disconnect":
            await ws.close()
            await harness.__aexit__(None, None, None)
        else:
            await ws.send_json({"type": "cancel", "turn_id": frames[-1]["turn_id"]})
            await asyncio.sleep(.1)
            await _close(harness, ws)
        assert fake.commands == []
    finally:
        if not ws.closed:
            await _close(harness, ws)


@pytest.mark.parametrize("name,refused", [("compress", False), ("status", False),
                                         ("reset", False), ("reset", True)])
@pytest.mark.parametrize("voice_hooks", [True, False], ids=["processing-hooks", "handler-only"])
async def test_tool_uses_the_real_gateway_command_policy(tmp_path, monkeypatch, device_env, name, refused, voice_hooks):
    """Real GatewayRunner, admission, slash dispatch and session reset; only inference/audio are fake."""
    from agent.i18n import t
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_results import execution_result

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    fake = ToolHermes(name)
    streamer = _FakeStreamer([b"\x01\x02"])
    _patch_speech(monkeypatch, streamer)
    harness = Harness(fake)
    fake.hub = harness.route.hub
    runner = GatewayRunner(GatewayConfig())
    harness.adapter.gateway_runner = runner
    runner.adapters = {Platform("converse"): harness.adapter}
    await asyncio.wait_for(initialize_session_authority(
        runner, profile_id="default", instance_id="command-tool-test"), 10)
    runner._wire_adapter_handlers(harness.adapter)
    if not voice_hooks:
        start, complete = harness.adapter.on_processing_start, harness.adapter.on_processing_complete

        async def command_start(event):
            if event.get_command():
                await start(event)

        async def command_complete(event, outcome):
            if event.get_command():
                await complete(event, outcome)

        monkeypatch.setattr(harness.adapter, "on_processing_start", command_start)
        monkeypatch.setattr(harness.adapter, "on_processing_complete", command_complete)
    if refused:
        check_access = runner._check_slash_access
        monkeypatch.setattr(runner, "_check_slash_access", lambda source, command:
                            "Command access denied." if command == "new" else check_access(source, command))

    async def agent(event, source, quick_key, run_generation):
        fake.results.append(await asyncio.to_thread(fake._call_tool))
        captured = execution_result.get()
        if captured is not None:
            captured["result"] = {"final_response": "Done.", "messages": []}
        return "Done."

    monkeypatch.setattr(runner, "_handle_message_with_agent", agent)
    client = await harness.__aenter__()
    ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
    try:
        await ws.send_str(_start_msg())
        assert (await ws.receive_json())["type"] == "ready"
        (chat_id,) = fake.hub._attachments
        device_env["HERMES_SESSION_CHAT_ID"] = chat_id
        source = harness.adapter.source_for(chat_id=chat_id, title="Test", recipient_id="test", device_name="Test")
        key = harness.adapter._source_session_key(source)
        old_session = runner.session_store.peek_session_id(key)
        for pcm in _speech_then_silence_pcm():
            await ws.send_bytes(pcm)
        frames = await _frames_until(ws, "approval_request")
        await ws.send_json({"type": "approval_response", "approval_id": frames[-1]["approval_id"], "choice": "once"})
        frames = await _frames_until(ws, "turn_done")
        assert len(fake.results) == 1 and fake.results[0] != "failed"
        if name == "compress":
            assert fake.results == [t("gateway.busy.slash_rejected", command="compress")]
        elif name == "status":
            assert fake.results[0] and fake.results[0] not in " ".join(streamer.requests)
        else:
            assert runner.session_store.peek_session_id(key) == old_session
        await ws.send_json({"type": "state", "turn_id": frames[-1]["turn_id"], "state": "listening"})
        if name == "reset":
            if refused:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.receive(), .15)
                assert runner.session_store.peek_session_id(key) == old_session and not ws.closed
            else:
                msg = await _recv(ws)
                assert msg.type == web.WSMsgType.CLOSE and ws.close_code == 4410
                assert runner.session_store.peek_session_id(key) != old_session
                from tools import slash_confirm
                assert slash_confirm.get_pending(key) is None
                assert runner._read_user_config().get("approvals", {}).get("destructive_slash_confirm", True)
    finally:
        await ws.close()
        await harness.__aexit__(None, None, None)
