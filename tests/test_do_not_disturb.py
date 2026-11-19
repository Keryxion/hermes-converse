"""Do Not Disturb (specs/server.md, "Do Not Disturb"; specs/client.md, section 12): the per-device
setting in ``devices.db``, the ``do_not_disturb`` frame both ways, the agent's tool, and what a turn
the server starts may send while it is on."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from aiohttp import web

pytest.importorskip("gateway.platforms.base")

from gateway.platforms.event import MessageEvent, MessageType  # noqa: E402

from hermes_converse import approvals as approvals_module  # noqa: E402
from hermes_converse.approvals import Approvals  # noqa: E402
from hermes_converse.conversation import ConversationHub  # noqa: E402
from hermes_converse.countdowns import Countdowns  # noqa: E402
from hermes_converse.loop import voice_system_prompt  # noqa: E402
from hermes_converse.named import DeviceStore  # noqa: E402
from hermes_converse.server_turn import ServerTurn  # noqa: E402
from hermes_converse.voice_tools import SCHEMAS, make_handlers  # noqa: E402
from tests.support.fake_hermes import FakeHermes  # noqa: E402
from tests.test_route import (API_KEY, Harness, _FakeStreamer, _patch_speech, _recv,  # noqa: E402
                              _speech_then_silence_pcm, _start_msg)

ON = {"type": "do_not_disturb", "on": True}
OFF = {"type": "do_not_disturb", "on": False}


class ScriptedHermes(FakeHermes):
    """A turn the server starts (message id ``wake-…``) calls the scripted tools, as the agent
    does from a worker thread, then replies."""

    def __init__(self) -> None:
        super().__init__(API_KEY, deltas=("Sure.",))
        self.hub = None
        self.calls: list[tuple[str, dict]] = []
        self.results: list[dict] = []
        self.contexts: list = []
        self.reply = "Time to stretch."
        self.finished = asyncio.Event()

    async def handle(self, event):
        # What Hermes's pre_llm_call hook adds to this turn, as the agent sees it.
        if not event.get_command():
            self.contexts.append(self.adapter.do_not_disturb_context(
                session_id=self.store.peek_session_id(self.adapter._source_session_key(event.source)),
                platform="converse"))
        if not str(event.message_id or "").startswith(("wake-", "countdown-ring-")):
            return await super().handle(event)
        handlers = make_handlers(self.hub)
        for name, args in self.calls:
            self.results.append(json.loads(await asyncio.to_thread(handlers[name], args)))
        self.turns.append({"text": event.text, "output": self.reply, "status": "completed"})
        self.finished.set()
        return self.reply


@pytest.fixture
def device_env(monkeypatch):
    env = {"HERMES_SESSION_PLATFORM": "converse", "HERMES_SESSION_CHAT_ID": ""}
    monkeypatch.setattr("gateway.session_context.get_session_env", lambda key, default="": env.get(key, default))
    return env


class Rig:
    """One device on a real socket, with a devices.db that outlives the harness."""

    def __init__(self, monkeypatch, tmp_path, device_env):
        self.monkeypatch, self.env = monkeypatch, device_env
        self.db = tmp_path / "devices.db"
        self.fake = ScriptedHermes()
        self.streamer = _FakeStreamer([b"\x01\x02"])
        self.wakes = 0

    async def connect(self):
        """A fresh route and device store, as after a gateway restart. Returns the socket and the
        first frame after ``ready``."""
        _patch_speech(self.monkeypatch, self.streamer, transcript="what time is it")
        self.harness = Harness(self.fake, device_store=DeviceStore(self.db), do_not_disturb_frames=True)
        self.hub = self.fake.hub = self.harness.route.hub
        self.client = await self.harness.__aenter__()
        self.ws = await self.client.ws_connect("/v1/audio/converse")
        await self.ws.send_str(_start_msg(key=API_KEY))
        assert (await self.ws.receive_json(timeout=3))["type"] == "ready"
        first = await self.ws.receive_json(timeout=3)
        (self.env["HERMES_SESSION_CHAT_ID"],) = self.hub._attachments
        self.chat_id = self.env["HERMES_SESSION_CHAT_ID"]
        self.attachment = self.hub.attachment(self.chat_id)
        return first

    async def close(self):
        if not self.ws.closed:
            await self.ws.send_str(json.dumps({"stop": True}))
            await self.ws.close()
        await self.harness.__aexit__(None, None, None)

    def stored(self) -> bool:
        with sqlite3.connect(self.db) as db:
            return json.loads(db.execute("SELECT settings FROM devices").fetchone()[0]).get("do_not_disturb") is True

    async def device_sets(self, on: bool):
        await self.ws.send_json({"type": "do_not_disturb", "on": on})
        for _ in range(200):
            if self.hub.do_not_disturb(self.chat_id) is on:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the device's frame was not applied")

    async def server_turn(self, *calls, reply="Time to stretch."):
        """Run one turn the server starts, to its end."""
        self.wakes += 1
        self.fake.calls, self.fake.reply = list(calls), reply
        self.fake.results = []
        self.fake.finished.clear()
        source = self.attachment and self.harness.adapter.source_for(
            chat_id=self.chat_id, title="Arya: Test Echo", recipient_id="r", device_name="Test Echo")
        await self.harness.adapter.handle_message(MessageEvent(
            text="[wake]", message_type=MessageType.TEXT, source=source, message_id=f"wake-{self.wakes}",
            internal=True))
        await asyncio.wait_for(self.fake.finished.wait(), 5)
        for _ in range(200):  # the gateway sends the final after the handler returns
            if self.chat_id not in self.harness.adapter._server_turns:
                break
            await asyncio.sleep(0.01)
        return self.fake.results

    async def spoken_turn(self):
        """The frame types and PCM chunk count of the turn the person's speech starts."""
        types, pcm = [], 0
        while True:
            msg = await _recv(self.ws, 8.0)
            if msg.type == web.WSMsgType.BINARY:
                pcm += 1
            elif msg.type == web.WSMsgType.TEXT:
                frame = json.loads(msg.data)
                types.append(frame["type"])
                if frame["type"] == "turn_done":
                    await self.ws.send_json({"type": "state", "turn_id": frame["turn_id"], "state": "listening"})
                    return types, pcm

    async def frames_until_said(self, text="Marker.", *, replies=0):
        """Everything the device receives up to the end of a ``say`` it sends now: what earlier
        server-started turns sent (*replies* of them spoken), then the marker's own frames."""
        await self.ws.send_json({"type": "say", "text": text})
        frames, pcm = [], 0
        while True:
            msg = await _recv(self.ws, 8.0)
            if msg.type == web.WSMsgType.BINARY:
                pcm += 1
                continue
            assert msg.type == web.WSMsgType.TEXT, msg
            frames.append(json.loads(msg.data))
            if frames[-1]["type"] == "turn_done":
                await self.ws.send_json({"type": "state", "turn_id": frames[-1]["turn_id"], "state": "listening"})
                if replies == 0:
                    return [f for f in frames if f["type"] not in ("thinking", "state")], pcm
                replies -= 1


@pytest.fixture
async def rig(monkeypatch, tmp_path, device_env):
    rig = Rig(monkeypatch, tmp_path, device_env)
    yield rig
    await rig.close()


# ── the setting and its frame ──

async def test_the_setting_follows_ready_and_is_off_for_a_device_that_never_set_it(rig):
    assert await rig.connect() == OFF
    assert not rig.stored()


async def test_a_client_that_never_sends_the_frame_is_served_as_before(rig):
    """Today's clients: the frame after ``ready`` is one more frame to ignore, and turns are spoken."""
    await rig.connect()
    for frame in _speech_then_silence_pcm():
        await rig.ws.send_bytes(frame)
    types, pcm = await rig.spoken_turn()
    assert "speaking" in types and pcm and "do_not_disturb" not in types
    await rig.server_turn()
    frames, _ = await rig.frames_until_said(replies=1)
    assert [f["type"] for f in frames] == ["speaking", "turn_done", "speaking", "turn_done"]
    assert rig.streamer.requests[-2:] == ["Time to stretch.", "Marker."]


async def test_the_devices_frame_sets_it_without_an_echo_and_it_survives_a_reconnect(rig):
    await rig.connect()
    await rig.device_sets(True)
    assert rig.stored()
    frames, _ = await rig.frames_until_said()
    assert "do_not_disturb" not in [f["type"] for f in frames]  # the device's own frame is not echoed
    await rig.close()
    assert await rig.connect() == ON  # a new route and store read devices.db
    await rig.device_sets(False)
    assert not rig.stored()


@pytest.mark.parametrize("on", ["true", 1, None])
async def test_a_frame_without_a_boolean_changes_nothing(rig, on):
    await rig.connect()
    await rig.ws.send_json({"type": "do_not_disturb", "on": on})
    frames, _ = await rig.frames_until_said()
    assert not rig.stored() and [f["type"] for f in frames] == ["speaking", "turn_done"]


async def test_the_tool_sets_it_and_tells_the_device_only_when_it_changed(rig):
    await rig.connect()
    handler = make_handlers(rig.hub)["do_not_disturb"]
    assert json.loads(await asyncio.to_thread(handler, {"on": True})) == {"result": "ok"}
    assert await rig.ws.receive_json(timeout=3) == ON
    assert rig.stored()
    assert json.loads(await asyncio.to_thread(handler, {"on": True})) == {"result": "ok"}
    assert json.loads(await asyncio.to_thread(handler, {"on": False})) == {"result": "ok"}
    assert await rig.ws.receive_json(timeout=3) == OFF  # the unchanged call in between sent nothing
    assert not rig.stored()
    assert "error" in json.loads(await asyncio.to_thread(handler, {"on": "yes"}))


async def test_the_tool_sets_it_for_a_device_that_is_not_attached(rig, device_env):
    await rig.connect()
    await rig.close()
    handler = make_handlers(rig.hub)["do_not_disturb"]
    assert json.loads(await asyncio.to_thread(handler, {"on": True})) == {"result": "ok"}
    assert rig.stored()
    assert await rig.connect() == ON
    device_env["HERMES_SESSION_CHAT_ID"] = "0" * 32
    assert json.loads(await asyncio.to_thread(handler, {"on": False})) == {"result": "not_a_device"}
    device_env["HERMES_SESSION_PLATFORM"] = "telegram"
    assert json.loads(await asyncio.to_thread(handler, {"on": False})) == {"result": "not_a_device"}
    assert rig.stored()


def test_an_existing_devices_db_gains_settings_and_keeps_unknown_ones(tmp_path):
    path = tmp_path / "devices.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE devices (device_name TEXT PRIMARY KEY, recipient_id TEXT NOT NULL UNIQUE);
            CREATE TABLE conversations (recipient_id TEXT NOT NULL, assistant_name TEXT NOT NULL,
                title TEXT NOT NULL UNIQUE, PRIMARY KEY(recipient_id, assistant_name));
            INSERT INTO devices VALUES ('Den Echo', '8f0e3c0e-63f1-4f0a-9d53-0a9a3b9d3a11');
        """)
    store = DeviceStore(path)
    device = store.reserve("Arya", "Den Echo")
    with sqlite3.connect(path) as db:  # a later release's setting, which this one keeps
        db.execute("""UPDATE devices SET settings='{"volume": 3}'""")
    assert device.recipient_id == "8f0e3c0e-63f1-4f0a-9d53-0a9a3b9d3a11"
    assert not store.do_not_disturb(device.recipient_id)
    assert store.set_do_not_disturb(device.recipient_id, True)
    assert not store.set_do_not_disturb(device.recipient_id, True)
    with sqlite3.connect(path) as db:
        assert json.loads(db.execute("SELECT settings FROM devices").fetchone()[0]) == {
            "volume": 3, "do_not_disturb": True}
    assert store.recipient_of(device.candidate_id.removeprefix("voice_")) == device.recipient_id
    store.close()
    again = DeviceStore(path)
    assert again.reserve("Sakura", "Den Echo").recipient_id == device.recipient_id  # one setting per device
    assert again.do_not_disturb(device.recipient_id)
    again.close()


# ── what a turn the server starts sends while it is on ──

async def test_a_reminder_during_do_not_disturb_sends_no_speech_or_tones(rig):
    await rig.connect()
    await rig.device_sets(True)
    results = await rig.server_turn(("play_tone", {"tone": "confirm"}))
    assert results == [{"result": "do_not_disturb"}]
    frames, pcm = await rig.frames_until_said()
    assert [f["type"] for f in frames] == ["speaking", "turn_done"] and pcm == 1  # only the marker
    assert rig.streamer.requests == ["Marker."]
    assert rig.fake.turns[-1]["output"] == "Time to stretch."  # the turn ran; its reply is Hermes's history
    # Nothing was queued: turning it off speaks nothing held earlier.
    await rig.device_sets(False)
    frames, _ = await rig.frames_until_said("Again.")
    assert [f["type"] for f in frames] == ["speaking", "turn_done"]
    assert rig.streamer.requests == ["Marker.", "Again."]


async def test_speak_this_in_a_server_started_turn_is_held_too(rig):
    await rig.connect()
    await rig.device_sets(True)
    assert await rig.server_turn(("speak_this", {"text": "Stretch now."})) == [{"result": "ok"}]
    await rig.frames_until_said()
    assert rig.streamer.requests == ["Marker."]


async def test_the_persons_own_request_is_answered_aloud(rig):
    await rig.connect()
    await rig.device_sets(True)
    for frame in _speech_then_silence_pcm():
        await rig.ws.send_bytes(frame)
    types, pcm = await rig.spoken_turn()
    assert "speaking" in types and pcm and rig.streamer.requests == ["Sure."]


async def test_a_turn_that_turns_it_off_is_spoken_and_one_that_turns_it_on_is_not(rig):
    """Held or not is decided when the reply is ready: the scheduled end of Do Not Disturb says
    good morning, and its scheduled start does not announce itself."""
    await rig.connect()
    assert await rig.server_turn(("do_not_disturb", {"on": True}), reply="Do Not Disturb is on.") == [{"result": "ok"}]
    assert await rig.ws.receive_json(timeout=3) == ON
    frames, _ = await rig.frames_until_said()
    assert [f["type"] for f in frames] == ["speaking", "turn_done"] and rig.streamer.requests == ["Marker."]
    assert rig.stored()

    await rig.server_turn(("do_not_disturb", {"on": False}), ("play_tone", {"tone": "confirm"}),
                          reply="Good morning.")
    assert rig.fake.results == [{"result": "ok"}, {"result": "ok"}]
    frames, _ = await rig.frames_until_said("Again.", replies=1)
    assert [f["type"] for f in frames] == ["do_not_disturb", "tone", "speaking", "turn_done", "speaking", "turn_done"]
    assert frames[0] == OFF and rig.streamer.requests == ["Marker.", "Good morning.", "Again."]


async def test_a_countdown_rings_and_its_still_ringing_turn_is_silent(rig):
    await rig.connect()
    await rig.device_sets(True)
    countdowns = rig.hub.countdowns = Countdowns(rig.hub)
    countdowns.attached(rig.attachment, rig.harness.adapter.source_for(
        chat_id=rig.chat_id, title="Arya: Test Echo", recipient_id="r", device_name="Test Echo"),
        900, asyncio.get_running_loop())
    made = json.loads(await asyncio.to_thread(countdowns.handle, {"action": "set", "in_seconds": 0.05}))
    frame = await rig.ws.receive_json(timeout=3)
    assert frame["type"] == "countdown" and frame["countdown_id"] == made["countdown_id"]  # it will ring
    await asyncio.sleep(0.1)
    await rig.ws.send_json({"type": "countdown", "countdown_id": made["countdown_id"], "ringing": True})
    device = countdowns._devices[rig.chat_id]
    for _ in range(200):
        if device.ring_timers:
            break
        await asyncio.sleep(0.01)
    rig.fake.reply = "Your timer is still ringing."
    rig.fake.finished.clear()
    threading.Thread(target=countdowns._still_ringing, args=(rig.chat_id, device.ring_generation)).start()
    await asyncio.wait_for(rig.fake.finished.wait(), 5)
    frames, _ = await rig.frames_until_said()
    assert [f["type"] for f in frames] == ["speaking", "turn_done"]  # no speech, and no countdown_cancel
    assert rig.streamer.requests == ["Marker."]
    listed = json.loads(await asyncio.to_thread(countdowns.handle, {"action": "list"}))["countdowns"]
    assert [c["state"] for c in listed] == ["ringing"]
    for timer in device.ring_timers + [t for c in device.countdowns.values() for t in c.timers]:
        timer.cancel()


async def test_a_server_started_turn_is_told_so_it_can_notify_another_way(rig):
    from hermes_converse.platform_adapter import DO_NOT_DISTURB_CONTEXT

    await rig.connect()
    await rig.device_sets(True)
    for frame in _speech_then_silence_pcm():  # the person's own turn is not told
        await rig.ws.send_bytes(frame)
    await rig.spoken_turn()
    await rig.server_turn()
    await rig.device_sets(False)
    await rig.server_turn()
    assert rig.fake.contexts == [None, {"context": DO_NOT_DISTURB_CONTEXT}, None]
    assert "another way" in DO_NOT_DISTURB_CONTEXT
    assert rig.harness.adapter.do_not_disturb_context(session_id="s", platform="telegram") is None


# ── tones and approvals, by who started the turn ──

class _Session:
    def __init__(self):
        self.stopped = False
        self.outputs = []
        self.transcripts = SimpleNamespace(put=self.outputs.append)
        self.answer_hook = None


@pytest.fixture
def den(tmp_path, device_env):
    """The hub with one attached device in Do Not Disturb, and no socket."""
    hub = ConversationHub()
    hub.devices = DeviceStore(tmp_path / "devices.db")
    device = hub.devices.reserve("Arya", "Den Echo")
    session, frames = _Session(), []
    attachment = hub.attach("den", session, "Arya", device.recipient_id)
    attachment.send = frames.append
    hub.devices.set_do_not_disturb(device.recipient_id, True)
    device_env["HERMES_SESSION_CHAT_ID"] = "den"
    yield SimpleNamespace(hub=hub, session=session, frames=frames, attachment=attachment)
    hub.devices.close()


def test_a_tone_plays_in_the_persons_turn_and_not_in_the_servers(den):
    den.hub.gateway = SimpleNamespace(_turns={}, _server_turns={"den": ServerTurn()})
    play_tone = make_handlers(den.hub)["play_tone"]
    assert json.loads(play_tone({"tone": "error"})) == {"result": "do_not_disturb"}
    assert den.frames == []
    den.hub.gateway._turns["den"] = SimpleNamespace(started=True, outcome=None)
    assert json.loads(play_tone({"tone": "error"})) == {"result": "ok"}
    assert den.frames == [{"type": "tone", "tone": "error"}]


PROMPT = SimpleNamespace(request_id="r1", session_key="k", chat_id="den", command="nc 10.0.4.17 6053",
                         description="Network connection (to a device)", choices=["once", "deny"])


@pytest.fixture
def asking(den, monkeypatch):
    resolved = []
    monkeypatch.setattr("tools.approval.resolve_gateway_approval",
                        lambda key, choice, request_id=None: resolved.append(choice) or 1)
    monkeypatch.setattr("tools.approval.list_gateway_approvals", lambda key: [{"request_id": "r1"}])
    monkeypatch.setattr(approvals_module, "_aux", lambda system, user, n: "open a network connection")
    den.adapter = SimpleNamespace(_hub=den.hub, _turns={})
    den.approvals, den.resolved = Approvals(den.adapter), resolved
    return den


async def _asked(den):
    await den.approvals.begin(PROMPT)
    for _ in range(200):
        if den.frames:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no approval_request")


async def test_an_approval_in_a_server_started_turn_is_denied_with_nothing_sent(asking):
    await asking.approvals.begin(PROMPT)
    await asyncio.sleep(0.05)
    assert asking.resolved == ["deny"] and "den" not in asking.approvals._pending
    assert asking.frames == [] and asking.session.outputs == [] and asking.session.answer_hook is None


async def test_the_command_tools_question_in_a_server_started_turn_is_denied(asking):
    assert await asking.approvals.ask("den", "/new", "Arya wants to start over. Okay?", 5) == "deny"
    assert asking.frames == [] and asking.session.outputs == []


async def test_a_pending_approval_is_denied_when_a_device_in_do_not_disturb_attaches(asking):
    asking.hub.detach("den", asking.attachment)
    await asking.approvals.begin(PROMPT)  # no device: held
    for _ in range(200):
        if asking.approvals._pending.get("den") and asking.approvals._pending["den"].summary:
            break
        await asyncio.sleep(0.01)
    assert asking.resolved == []
    attachment = asking.hub.attach("den", asking.session, "Arya", asking.attachment.recipient_id)
    attachment.send = asking.frames.append
    asking.approvals.attached(attachment)
    assert asking.resolved == ["deny"] and asking.frames == []


async def test_an_approval_in_the_persons_turn_is_asked_aloud(asking):
    said = []
    asking.adapter._turns["den"] = SimpleNamespace(started=True, outcome=None, stopped=False, detached=False,
                                                   on_delta=said.append, progress_at=0.0)
    await _asked(asking)
    assert [f["type"] for f in asking.frames] == ["approval_request", "tone"]
    assert said == ["Arya wants to open a network connection. Okay? Say yes or no.\n"]
    assert asking.session.answer_hook is not None
    asking.approvals._settle(asking.approvals._pending["den"], "deny", "denied")


async def test_what_follows_a_held_reply_follows_the_turn(den):
    """An approved ``command`` for a new conversation waits for the reply to play; a held reply never plays."""
    from gateway.config import PlatformConfig
    from hermes_converse.platform_adapter import ConverseAdapter

    adapter = ConverseAdapter(PlatformConfig(enabled=True), den.hub, approval_choice=lambda: "deny")
    ran = asyncio.Event()

    async def after():
        ran.set()

    source = adapter.source_for(chat_id="den", title="Arya: Den Echo", recipient_id="r", device_name="Den Echo")
    event = MessageEvent(text="[wake]", message_type=MessageType.TEXT, source=source, message_id="wake-1")
    await adapter.on_processing_start(event)
    adapter._server_turns["den"].after_playback = after
    assert (await adapter.send("den", "Starting over.", metadata={"notify": True})).success
    assert den.session.outputs == [] and not ran.is_set()
    from gateway.platforms.event import ProcessingOutcome
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    await asyncio.wait_for(ran.wait(), 2)


# ── what the model is told ──

def test_the_tool_and_the_prompt_say_how_to_end_it_at_a_time():
    schema = SCHEMAS["do_not_disturb"]
    assert schema["parameters"] == {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]}
    assert "nothing ends it by time" in schema["description"]
    prompt = voice_system_prompt("Arya")
    assert '{"kind":"at","at":"07:00"}' in prompt and "owner_asked true" in prompt
    assert '{"kind":"cron","cron":"0 22 * * *"}' in prompt and '"0 7 * * *"' in prompt and "role true" in prompt
    assert "notify false" in prompt and "quiet" not in prompt.lower()
