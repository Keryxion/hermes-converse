"""The converse gateway platform against Hermes's real ``BasePlatformAdapter``.

The message handler below stands in for the gateway runner: it is what ``handle_message`` hands an
admitted event to, and what returns the turn's final reply.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("gateway.platforms.base")

from gateway.config import PlatformConfig  # noqa: E402

from hermes_converse.conversation import ConversationHub  # noqa: E402
from hermes_converse.platform_adapter import ConverseAdapter, GatewayTurns, register_platform  # noqa: E402
from tests.support.fake_hermes import FakeStore  # noqa: E402

NOTIFY = {"notify": True}


class Device:
    """A live socket as the hub sees it."""

    def __init__(self):
        self.stopped = False
        self.outputs = []
        self.transcripts = SimpleNamespace(put=self.outputs.append)


@pytest.fixture
async def rig():
    hub = ConversationHub()
    adapter = ConverseAdapter(PlatformConfig(enabled=True), hub, approval_choice=lambda: "deny")
    store = FakeStore()
    adapter.set_session_store(store)
    assert await adapter.connect()
    yield SimpleNamespace(hub=hub, adapter=adapter, store=store)
    await adapter.cancel_background_tasks()
    await adapter.disconnect()


async def _turns(rig, chat_id="den"):
    source = rig.adapter.source_for(chat_id=chat_id, title="Arya: Den Echo", recipient_id="r-" + chat_id,
                                    device_name="Den Echo")
    key = await rig.adapter.bind(source)
    return GatewayTurns(rig.adapter, source, key, settle_timeout=2.0), key


async def test_connect_links_the_hub_and_disconnect_unlinks(rig):
    assert rig.hub.gateway is rig.adapter
    await rig.adapter.disconnect()
    assert rig.hub.gateway is None


async def test_streamed_reply_is_spoken_once_and_final_is_the_result(rig):
    turns, key = await _turns(rig)
    seen = {}

    async def handler(event):
        seen.update(text=event.text, prompt=event.channel_prompt, control=event.allow_gateway_control,
                    chat=event.source.chat_id, name=event.source.chat_name)
        sid = rig.store.peek_session_id(key)
        rig.adapter.on_stream_start(session_id=sid)
        for part in ("It is ", "sunny."):
            rig.adapter.on_stream_delta(delta=part, kind="text", session_id=sid)
        rig.adapter.on_stream_delta(delta="secret", kind="reasoning", session_id=sid)
        rig.adapter.on_tool_call(session_id=sid)
        rig.adapter.on_api_request(session_id=sid, usage={"reasoning_tokens": 40})
        rig.adapter.on_api_request(session_id=sid, usage={"completion_tokens_details": {"reasoning_tokens": 2}})
        return "It is sunny."

    rig.adapter.set_message_handler(handler)
    spoken = []
    result = await turns.run_turn(text="weather?", on_delta=spoken.append, instructions="be brief")
    assert (result.reply, result.error, result.status) == ("It is sunny.", None, "completed")
    assert spoken == ["It is ", "sunny."]
    assert seen == {"text": "weather?", "prompt": "be brief", "control": False, "chat": "den",
                    "name": "Arya: Den Echo"}
    assert (turns.tool_calls, turns.reasoning_tokens) == (1, 42)
    assert turns.settled


async def test_unstreamed_final_is_spoken(rig):
    turns, _ = await _turns(rig)

    async def handler(event):
        return "Four."

    rig.adapter.set_message_handler(handler)
    spoken = []
    result = await turns.run_turn(text="2+2", on_delta=spoken.append)
    assert spoken == ["Four."] and result.reply == "Four."


async def test_handler_failure_without_processing_hooks_settles_the_voice_turn(rig, monkeypatch):
    """The busy observer reports errors without calling on_processing_complete."""
    turns, _ = await _turns(rig)

    async def handler(event):
        raise RuntimeError("gateway failed")

    async def observer(event, session_key):
        try:
            await rig.adapter._message_handler(event)
        except RuntimeError:
            pass  # the real busy observer logs/notifies the handler error
        finally:
            rig.adapter._active_sessions.pop(session_key, None)

    rig.adapter.set_message_handler(handler)
    monkeypatch.setattr(rig.adapter, "_process_message_background", observer)
    spoken = []
    result = await asyncio.wait_for(turns.run_turn(text="2+2", on_delta=spoken.append), 2)
    assert result.status == "failed" and result.error == "agent run failed"
    assert not spoken and turns.settled and not rig.adapter._abandoned


async def test_turn_replayed_by_the_busy_queue_is_still_spoken(rig, monkeypatch):
    """A turn that reaches a still-busy chat (right after our own /model command) waits in the
    gateway's durable queue, which replays it as a new event whose id is the admission id."""
    turns, key = await _turns(rig)
    handle = rig.adapter.handle_message

    async def replay(event):
        await handle(dataclasses.replace(event, message_id="admission-1"))

    monkeypatch.setattr(rig.adapter, "handle_message", replay)

    async def handler(event):
        assert event.message_id == "admission-1"
        sid = rig.store.peek_session_id(key)
        rig.adapter.on_stream_start(session_id=sid)
        rig.adapter.on_stream_delta(delta="Nick is ", session_id=sid)
        return "Nick is at home."

    rig.adapter.set_message_handler(handler)
    spoken = []
    result = await asyncio.wait_for(turns.run_turn(text="where's Nick?", on_delta=spoken.append), 5)
    assert spoken == ["Nick is ", "at home."]
    assert (result.reply, result.status) == ("Nick is at home.", "completed") and turns.settled


async def test_replay_of_different_text_is_not_adopted(rig, monkeypatch):
    turns, _ = await _turns(rig)
    handle = rig.adapter.handle_message

    async def replay(event):
        await handle(dataclasses.replace(event, message_id="admission-2", text="something else"))

    monkeypatch.setattr(rig.adapter, "handle_message", replay)

    async def handler(event):
        return "Not yours."

    rig.adapter.set_message_handler(handler)
    spoken = []
    turns._settle_timeout = 0.2
    with pytest.raises(TimeoutError):  # the voice turn never sees a reply that is not its own
        await asyncio.wait_for(turns.run_turn(text="where's Nick?", on_delta=spoken.append), 0.5)
    assert spoken == []


async def test_final_speaks_only_what_the_stream_missed(rig):
    turns, key = await _turns(rig)

    async def handler(event):
        sid = rig.store.peek_session_id(key)
        rig.adapter.on_stream_start(session_id=sid)
        rig.adapter.on_stream_delta(delta="Checking. ", session_id=sid)  # interim commentary
        rig.adapter.on_stream_start(session_id=sid)
        rig.adapter.on_stream_delta(delta="The door ", session_id=sid)
        return "The door is locked."

    rig.adapter.set_message_handler(handler)
    spoken = []
    await turns.run_turn(text="door?", on_delta=spoken.append)
    assert spoken == ["Checking. ", "The door ", "is locked."]


async def test_model_call_starting_before_the_final_send_does_not_repeat_the_reply(rig):
    """Hermes's background review starts its own model call in the session as soon as the reply
    is ready, before the reply is sent."""
    turns, key = await _turns(rig)

    async def handler(event):
        sid = rig.store.peek_session_id(key)
        rig.adapter.on_stream_start(session_id=sid)
        rig.adapter.on_stream_delta(delta="It is on TestFlight now.", session_id=sid)
        rig.adapter.on_stream_start(session_id=sid)  # the review's call
        return "It is on TestFlight now."

    rig.adapter.set_message_handler(handler)
    spoken = []
    await turns.run_turn(text="status?", on_delta=spoken.append)
    assert spoken == ["It is on TestFlight now."]


async def test_late_delta_after_the_final_is_not_spoken_twice(rig):
    turns, key = await _turns(rig)
    spoken = []

    async def handler(event):
        return "Done."

    rig.adapter.set_message_handler(handler)

    original = rig.adapter.send

    async def send(chat_id, content, reply_to=None, metadata=None):
        out = await original(chat_id, content, reply_to=reply_to, metadata=metadata)
        rig.adapter.on_stream_delta(delta="Done.", session_id=rig.store.peek_session_id(key))
        return out

    rig.adapter.send = send
    await turns.run_turn(text="go", on_delta=spoken.append)
    assert spoken == ["Done."]


async def test_silent_result_speaks_nothing(rig):
    turns, _ = await _turns(rig)
    spoken = []
    for reply in ("[SILENT]", None):
        async def handler(event, reply=reply):
            return reply

        rig.adapter.set_message_handler(handler)
        result = await turns.run_turn(text="tick", on_delta=spoken.append)
        assert (result.error, result.status) == (None, "completed")
    assert spoken == []


async def test_status_text_during_a_turn_is_not_spoken(rig):
    turns, _ = await _turns(rig)

    async def handler(event):
        sent = await rig.adapter.send("den", "⏳ Working…")
        assert sent.success
        return "Okay."

    rig.adapter.set_message_handler(handler)
    spoken = []
    await turns.run_turn(text="go", on_delta=spoken.append)
    assert spoken == ["Okay."]


@pytest.mark.parametrize('notify', [False, True])
async def test_interim_notices_never_become_proactive_speech(rig, notify):
    device = Device()
    rig.hub.attach("den", device)
    for content in ("Use /sethome to set your home channel.", "Skill review: updated a skill."):
        result = await rig.adapter.send(
            "den", content, metadata={"_interim_send": True, "notify": notify})
        assert result.success
    assert device.outputs == []
    assert (await rig.adapter.send("den", "Your reminder is due.", metadata=NOTIFY)).success
    assert len(device.outputs) == 1


async def test_interim_notice_cannot_finish_an_active_voice_turn(rig):
    turns, _ = await _turns(rig)

    async def handler(event):
        assert (await rig.adapter.send(
            "den", "Skill review completed.",
            metadata={"_interim_send": True, "notify": True})).success
        return "Timer set."

    rig.adapter.set_message_handler(handler)
    spoken = []
    result = await turns.run_turn(text="Set a timer", on_delta=spoken.append)
    assert result.reply == "Timer set." and spoken == ["Timer set."]


async def test_stop_interrupts_the_turn_and_drops_its_late_reply(rig):
    turns, key = await _turns(rig)
    started = asyncio.Event()

    async def handler(event):
        started.set()
        while not rig.adapter.has_pending_interrupt(key):  # the runner's interrupt monitor
            await asyncio.sleep(0.01)
        return "partial answer"

    rig.adapter.set_message_handler(handler)
    spoken = []
    task = asyncio.ensure_future(turns.run_turn(text="long story", on_delta=spoken.append))
    await started.wait()
    assert not turns.settled
    await turns.stop()
    result = await task
    assert (result.status, result.error) == ("interrupted", None)
    assert spoken == [] and turns.settled


async def test_cancelled_host_stops_the_turn(rig):
    turns, key = await _turns(rig)
    started, finished = asyncio.Event(), asyncio.Event()

    async def handler(event):
        started.set()
        while not rig.adapter.has_pending_interrupt(key):
            await asyncio.sleep(0.01)
        finished.set()
        return None

    rig.adapter.set_message_handler(handler)
    task = asyncio.ensure_future(turns.run_turn(text="x", on_delta=lambda _: None))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set() and turns.settled


async def test_turn_that_outlives_its_stop_never_blocks_or_speaks(rig):
    """A long tool call can outlast the stop. The device moves on; the next request is admitted
    behind it and the old turn's late reply is dropped."""
    turns, _ = await _turns(rig)
    turns._settle_timeout = 0.05
    started, finish = asyncio.Event(), asyncio.Event()

    async def handler(event):
        if event.text == "long":
            started.set()
            await finish.wait()
            return "stale answer"
        return "Back."

    rig.adapter.set_message_handler(handler)
    spoken = []
    rig.hub.attach("den", Device())
    first = asyncio.ensure_future(turns.run_turn(text="long", on_delta=spoken.append))
    await started.wait()
    await turns.stop()
    assert (await first).status == "interrupted" and not turns.settled
    second = asyncio.ensure_future(turns.run_turn(text="again", on_delta=spoken.append))
    await asyncio.sleep(0.05)
    finish.set()
    result = await second
    assert (result.status, spoken) == ("completed", ["Back."])
    assert rig.adapter._abandoned == {} and rig.hub._attachments["den"].session.outputs == []


@pytest.mark.parametrize("heard", [False, True])
async def test_barged_run_nobody_follows_is_delivered_only_if_unheard(rig, heard):
    """Barge-in leaves the run going. If the person's words never reach it (the capture was
    empty), its reply is delivered as a Telegram reply would be, unless some of it was heard."""
    turns, _ = await _turns(rig)
    device = Device()
    rig.hub.attach("den", device)
    started, finish = asyncio.Event(), asyncio.Event()

    async def handler(event):
        started.set()
        await finish.wait()
        return "Rinse with plain water."

    rig.adapter.set_message_handler(handler)
    spoken = []
    task = asyncio.ensure_future(turns.run_turn(text="my floors look gross", on_delta=spoken.append))
    await started.wait()
    turns.release(heard)
    result = await task
    assert (result.status, result.reply) == ("interrupted", "")
    finish.set()
    for _ in range(200):
        if turns.settled and not rig.adapter._processing:
            break
        await asyncio.sleep(0.01)
    expected = [] if heard else [("Rinse with plain water.", "listen", True)]
    assert [(o.text, o.next_action, o.unheard) for o in device.outputs] == expected
    assert spoken == []


async def test_words_queued_behind_a_barged_run_hear_its_reply_then_their_own(rig):
    """With no busy handler the gateway queues words that arrive mid-run (``busy_input_mode:
    queue``). The turn that follows the barged run speaks its reply, then the answer to the words."""
    turns, key = await _turns(rig)
    started, finish = asyncio.Event(), asyncio.Event()

    async def handler(event):
        sid = rig.store.peek_session_id(key)
        rig.adapter.on_stream_start(session_id=sid)
        if event.text == "my floors look gross":
            started.set()
            rig.adapter.on_stream_delta(delta="Rinse ", kind="text", session_id=sid)
            await finish.wait()
            rig.adapter.on_stream_delta(delta="with plain water.", kind="text", session_id=sid)
            return "Rinse with plain water."
        rig.adapter.on_stream_delta(delta="Which remote?", kind="text", session_id=sid)
        return "Which remote?"

    rig.adapter.set_message_handler(handler)
    first_spoken, spoken = [], []
    first = asyncio.ensure_future(turns.run_turn(text="my floors look gross", on_delta=first_spoken.append))
    await started.wait()
    turns.release(False)
    assert (await first).status == "interrupted"
    second = asyncio.ensure_future(turns.run_turn(text="can I put the remote somewhere", on_delta=spoken.append))
    await asyncio.sleep(0.05)
    finish.set()
    result = await second
    assert (result.status, result.reply) == ("completed", "Which remote?")
    assert first_spoken == ["Rinse "]
    assert spoken == ["Rinse with plain water.", "Which remote?"]
    assert turns.settled


async def test_words_folded_into_a_barged_run_hear_its_next_response(rig):
    """``busy_input_mode: interrupt`` redirects the running turn: the words become part of it and
    its next response answers them. The turn that follows speaks that response only."""
    turns, key = await _turns(rig)
    started, redirected = asyncio.Event(), asyncio.Event()

    async def busy(event, session_key):
        redirected.set()
        return True  # consumed by the running turn

    async def handler(event):
        sid = rig.store.peek_session_id(key)
        rig.adapter.on_stream_start(session_id=sid)
        started.set()
        rig.adapter.on_stream_delta(delta="Turning on ", kind="text", session_id=sid)
        await redirected.wait()
        rig.adapter.on_stream_delta(delta="the lights.", kind="text", session_id=sid)  # the cancelled request
        rig.adapter.on_stream_start(session_id=sid)  # the request again, with the words appended
        rig.adapter.on_stream_delta(delta="Kitchen lights on, warm white.", kind="text", session_id=sid)
        return "Kitchen lights on, warm white."

    rig.adapter.set_message_handler(handler)
    rig.adapter.set_busy_session_handler(busy)
    first_spoken, spoken = [], []
    first = asyncio.ensure_future(turns.run_turn(text="turn on the light", on_delta=first_spoken.append))
    await started.wait()
    turns.release(True)
    assert (await first).status == "interrupted"
    result = await turns.run_turn(text="the kitchen, warm white", on_delta=spoken.append)
    assert (result.status, result.reply) == ("completed", "Kitchen lights on, warm white.")
    assert first_spoken == ["Turning on "]
    assert spoken == ["Kitchen lights on, warm white."]
    assert turns.settled


async def test_failed_turn_reports_an_error(rig):
    turns, _ = await _turns(rig)

    async def handler(event):
        raise RuntimeError("boom")

    rig.adapter.set_message_handler(handler)
    result = await turns.run_turn(text="x", on_delta=lambda _: None)
    assert (result.status, result.error) == ("failed", "agent run failed")


async def test_unhandled_event_is_refused(rig):
    turns, _ = await _turns(rig)
    result = await turns.run_turn(text="x", on_delta=lambda _: None)  # no message handler installed
    assert result.status == "failed" and turns.settled


async def test_devices_do_not_hear_each_other(rig):
    den, den_key = await _turns(rig, "den")
    kitchen, kitchen_key = await _turns(rig, "kitchen")
    gate = asyncio.Event()

    async def handler(event):
        await gate.wait()
        return None

    rig.adapter.set_message_handler(handler)
    heard = {"den": [], "kitchen": []}
    tasks = [asyncio.ensure_future(den.run_turn(text="a", on_delta=heard["den"].append)),
             asyncio.ensure_future(kitchen.run_turn(text="b", on_delta=heard["kitchen"].append))]
    await asyncio.sleep(0.05)
    rig.adapter.on_stream_delta(delta="for den", session_id=rig.store.peek_session_id(den_key))
    rig.adapter.on_stream_delta(delta="for kitchen", session_id=rig.store.peek_session_id(kitchen_key))
    rig.adapter.on_stream_delta(delta="stray", session_id="unknown-session")
    gate.set()
    await asyncio.gather(*tasks)
    assert heard == {"den": ["for den"], "kitchen": ["for kitchen"]}


async def test_compaction_rotating_the_session_id_keeps_the_stream(rig):
    turns, key = await _turns(rig)

    async def handler(event):
        rig.adapter.on_stream_delta(delta="Before. ", session_id=rig.store.peek_session_id(key))
        rig.store.ids[key] = "sid-after-compaction"
        rig.adapter.on_stream_delta(delta="After.", session_id="sid-after-compaction")
        return "Before. After."

    rig.adapter.set_message_handler(handler)
    spoken = []
    await turns.run_turn(text="x", on_delta=spoken.append)
    assert spoken == ["Before. ", "After."]


async def test_proactive_reply_is_spoken_through_the_attached_device(rig):
    device = Device()
    rig.hub.attach("den", device)
    sent = await rig.adapter.send("den", "Your timer is done.", metadata=NOTIFY)
    assert sent.success
    assert [(o.text, o.next_action) for o in device.outputs] == [("Your timer is done.", None)]  # resolved on playback
    assert device.outputs[0].expires_at > time.time()


async def test_server_turn_explicit_standby_survives_publish(rig, monkeypatch):
    from hermes_converse.server_turn import ServerTurn
    from hermes_converse.voice_tools import make_handlers
    import json
    from gateway import session_context

    device = Device()
    rig.hub.attach("den", device)
    server_turn = ServerTurn()
    rig.adapter._server_turns["den"] = server_turn
    monkeypatch.setattr(session_context, "get_session_env", lambda key, default="": {
        "HERMES_SESSION_PLATFORM": "converse", "HERMES_SESSION_CHAT_ID": "den"}.get(key, default)
    )
    assert json.loads(make_handlers(rig.hub)["standby"]({})) == {"result": "ok"}
    assert server_turn.standby
    assert (await rig.adapter.send("den", "Going to standby.", metadata=NOTIFY)).success
    assert [(o.text, o.next_action) for o in device.outputs] == [("Going to standby.", "standby")]


async def test_server_turn_mute_survives_publish_and_last_call_wins(rig, monkeypatch):
    from hermes_converse.server_turn import ServerTurn
    from hermes_converse.voice_tools import make_handlers
    import json
    from gateway import session_context

    device = Device()
    rig.hub.attach("den", device)
    server_turn = ServerTurn()
    rig.adapter._server_turns["den"] = server_turn
    monkeypatch.setattr(session_context, "get_session_env", lambda key, default="": {
        "HERMES_SESSION_PLATFORM": "converse", "HERMES_SESSION_CHAT_ID": "den"}.get(key, default)
    )
    handlers = make_handlers(rig.hub)
    assert json.loads(handlers["mute"]({})) == {"result": "ok"}
    assert json.loads(handlers["standby"]({})) == {"result": "ok"}
    assert server_turn.standby and not server_turn.mute
    assert json.loads(handlers["mute"]({})) == {"result": "ok"}
    assert server_turn.mute and not server_turn.standby
    assert (await rig.adapter.send("den", "Muted.", metadata=NOTIFY)).success
    assert [(o.text, o.next_action) for o in device.outputs] == [("Muted.", "mute")]


async def test_server_standby_does_not_mark_earlier_voice_turn(rig, monkeypatch):
    from hermes_converse.server_turn import ServerTurn
    from hermes_converse.voice_tools import make_handlers
    from gateway import session_context
    import json

    device = Device()
    rig.hub.attach("den", device)
    voice = SimpleNamespace(standby=False, started=True, outcome=None)
    rig.adapter._turns["den"] = voice
    server_turn = ServerTurn()
    rig.adapter._server_turns["den"] = server_turn
    monkeypatch.setattr(session_context, "get_session_env", lambda key, default="": {
        "HERMES_SESSION_PLATFORM": "converse", "HERMES_SESSION_CHAT_ID": "den"}.get(key, default)
    )
    assert json.loads(make_handlers(rig.hub)["standby"]({})) == {"result": "ok"}
    assert server_turn.standby and not voice.standby


async def test_proactive_reply_without_a_device_fails_and_silence_succeeds(rig):
    missing = await rig.adapter.send("den", "Anyone there?", metadata=NOTIFY)
    assert not missing.success and "not_attached" in missing.error
    device = Device()
    rig.hub.attach("den", device)
    assert (await rig.adapter.send("den", "[SILENT]", metadata=NOTIFY)).success
    assert device.outputs == []


async def test_scheduled_turn_replies_in_the_same_conversation_without_a_voice_turn(rig):
    """A wake injected into the session runs like any message; its final is spoken, its status is not."""
    from gateway.platforms.event import MessageEvent, MessageType

    device = Device()
    rig.hub.attach("den", device)
    source = rig.adapter.source_for(chat_id="den", title="Arya: Den Echo", recipient_id="r", device_name="Den Echo")

    async def handler(event):
        await rig.adapter.send("den", "⏳ Working…")
        return "Time to stretch."

    rig.adapter.set_message_handler(handler)
    wake = MessageEvent(text="[wake]", message_type=MessageType.TEXT, source=source, message_id="wake-1",
                        internal=True)
    await rig.adapter.handle_message(wake)
    for _ in range(100):
        if device.outputs:
            break
        await asyncio.sleep(0.01)
    assert [o.text for o in device.outputs] == ["Time to stretch."]


async def test_model_selection_is_sent_as_the_gateway_model_command(rig):
    turns, key = await _turns(rig)
    seen = []

    async def handler(event):
        seen.append((event.text, event.get_command(), event.allow_gateway_control))
        if event.get_command() == "model":
            args = event.get_command_args().split()
            rig.store.models[key] = None if args == ["reset"] else {"model": args[0], "provider": args[2]}
            return "Model switched."
        return "ok"

    rig.adapter.set_message_handler(handler)
    spoken = []
    await turns.run_turn(text="a", on_delta=spoken.append)
    assert [text for text, _, _ in seen] == ["a"]  # no selection, nothing stored: no command
    await turns.run_turn(text="b", on_delta=spoken.append, model="flash", provider="litellm")
    await turns.run_turn(text="c", on_delta=spoken.append, model="flash", provider="litellm")
    await turns.run_turn(text="d", on_delta=spoken.append)
    assert [text for text, _, _ in seen] == [
        "a", "/model flash --provider litellm", "b", "c", "/model reset", "d"]
    assert seen[1][1:] == ("model", True) and seen[2][1:] == (None, False)
    assert spoken == ["ok"] * 4  # the command's confirmation is never spoken


async def test_command_runs_through_the_gateway_and_the_chat_follows_the_new_session(rig):
    turns, key = await _turns(rig)
    seen = []

    async def handler(event):
        seen.append(event.text)
        if event.get_command() == "new":
            rig.store.ids[key] = "sid-fresh"
            return "Started a new session."
        return "ok"

    rig.adapter.set_message_handler(handler)
    assert await turns.run_command("/new") == "Started a new session."
    assert seen == ["/new"]
    assert rig.store.peek_session_id(key) == "sid-fresh"  # History reads whatever session the key names


@pytest.mark.parametrize("busy", [False, True])
async def test_command_does_not_capture_other_tasks_output(rig, busy):
    turns, key = await _turns(rig)
    device = Device()
    rig.hub.attach("den", device)
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(event):
        entered.set()
        await release.wait()
        return "Command reply."

    rig.adapter.set_message_handler(handler)
    if busy:
        rig.adapter._active_sessions[key] = asyncio.Event()
    task = asyncio.create_task(turns.run_command("/status", stop=False))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await rig.adapter.send("den", "Other output.", metadata=NOTIFY)
        release.set()
        assert await asyncio.wait_for(task, 2) == "Command reply."
        assert [o.text for o in device.outputs] == ["Other output."]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        rig.adapter._active_sessions.pop(key, None)


async def test_queued_plugin_command_reply_is_not_spoken_later(rig):
    turns, key = await _turns(rig)
    device = Device()
    rig.hub.attach("den", device)
    rig.adapter.set_message_handler(lambda event: asyncio.sleep(0, result="Plugin reply."))
    rig.adapter._active_sessions[key] = asyncio.Event()
    try:
        assert await turns.run_command("/test-plugin", stop=False) is None
        capture = rig.adapter._commands["den"]
        assert not capture.started
        # The agent's own final still reaches its device while the command is queued.
        await rig.adapter.send("den", "Agent final.", metadata=NOTIFY)
        rig.adapter._active_sessions.pop(key)
        pending = rig.adapter.get_pending_message(key)
        assert pending.text == "/test-plugin"
        rig.adapter._start_session_processing(pending, key)
        await asyncio.wait_for(capture.done.wait(), 2)
        assert capture.reply == "Plugin reply."
        assert [o.text for o in device.outputs] == ["Agent final."]
        assert not rig.adapter._commands
    finally:
        rig.adapter._active_sessions.pop(key, None)


@pytest.mark.parametrize("confirmed_name,approved,runs", [
    ("new", True, True), ("new", False, False), ("reload-mcp", True, False),
])
async def test_voice_approval_only_confirms_the_same_command_once(rig, confirmed_name, approved, runs):
    from gateway.platforms.event import MessageEvent, MessageType
    from hermes_converse.platform_adapter import _Command
    from tools import slash_confirm

    turns, key = await _turns(rig)
    executed = []

    async def execute(choice):
        executed.append(choice)
        return "Reset done."

    command = _Command(MessageEvent(text="/reset", message_type=MessageType.COMMAND, source=turns._source),
                       started=True, approved=approved)
    rig.adapter._commands["den"] = command
    slash_confirm.register(key, "confirmation-1", confirmed_name, execute)
    try:
        result = await rig.adapter.send_slash_confirm("den", "Confirm", "Do it?", key, "confirmation-1")
        assert result.success == runs
        assert executed == (["once"] if runs else [])
        if runs:
            assert command.reply == "Reset done." and not command.approved
            assert slash_confirm.get_pending(key) is None
    finally:
        slash_confirm.clear(key)
        rig.adapter._commands.pop("den", None)


async def test_new_socket_clears_a_model_left_by_an_earlier_one(rig):
    turns, key = await _turns(rig)
    rig.store.models[key] = {"model": "terra", "provider": "litellm"}
    seen = []

    async def handler(event):
        seen.append(event.text)
        if event.get_command() == "model":
            rig.store.models[key] = None
        return "ok"

    rig.adapter.set_message_handler(handler)
    await turns.run_turn(text="a", on_delta=lambda _: None)
    assert seen == ["/model reset", "a"]


async def test_every_agent_event_counts_as_progress(rig):
    import time
    turns, key = await _turns(rig)
    seen = []

    async def handler(event):
        sid = rig.store.peek_session_id(key)
        for hook in (lambda: rig.adapter.on_tool_call(session_id=sid),
                     lambda: rig.adapter.on_tool_done(session_id=sid),
                     lambda: rig.adapter.on_api_request(session_id=sid, usage=None),
                     lambda: rig.adapter.on_stream_start(session_id=sid),
                     lambda: rig.adapter.on_stream_delta(delta="hm", kind="reasoning", session_id=sid)):
            before = turns.last_progress
            await asyncio.sleep(0.002)
            hook()
            seen.append(turns.last_progress > before)
        return "ok"

    assert turns.last_progress == 0.0
    rig.adapter.set_message_handler(handler)
    started = time.monotonic()
    await turns.run_turn(text="go", on_delta=lambda _: None)
    assert seen == [True] * 5 and turns.last_progress >= started


async def test_approval_and_clarify_prompts_are_answered_at_once(rig, monkeypatch):
    calls = []
    monkeypatch.setattr("tools.approval.resolve_gateway_approval",
                        lambda key, choice, **kw: calls.append((key, choice, kw.get("request_id"))) or 1)
    monkeypatch.setattr("tools.clarify_gateway.resolve_gateway_clarify",
                        lambda clarify_id, response: calls.append((clarify_id, response)) or True)
    prompt = SimpleNamespace(session_key="k", request_id="req-1")
    assert (await rig.adapter._send_exec_approval_prompt(prompt)).success
    assert (await rig.adapter.send_clarify("den", "Which?", ["a", "b"], "c-1", "k")).success
    assert calls == [("k", "deny", "req-1"), ("c-1", "")]


def test_register_platform_declares_a_trusted_text_quiet_platform():
    registered, hooks = {}, {}
    ctx = SimpleNamespace(register_platform=lambda **kw: registered.update(kw),
                          register_hook=lambda name, fn: hooks.update({name: fn}),
                          get_config=lambda key, default=None: default)
    hub = ConversationHub()
    assert register_platform(ctx, hub)
    assert registered["name"] == "converse" and registered["trusted_inbound"] is True
    assert registered["display_overrides"]["streaming"] is False
    assert registered["is_connected"](None) and registered["check_fn"]()
    assert set(hooks) == {"on_stream_start", "on_stream_delta", "pre_tool_call", "post_tool_call",
                          "post_api_request", "pre_llm_call"}
    hooks["on_stream_delta"](delta="x", session_id="s")  # no adapter connected: ignored
    adapter = registered["adapter_factory"](PlatformConfig(enabled=True))
    assert isinstance(adapter, ConverseAdapter) and adapter._approval_choice() == "deny"


def test_register_platform_reports_an_unsupporting_hermes():
    def refuse(**kw):
        raise TypeError("unexpected keyword")

    ctx = SimpleNamespace(register_platform=refuse, register_hook=lambda *a: pytest.fail("no hooks"))
    assert register_platform(ctx, ConversationHub()) is False
