"""The converse gateway platform against Hermes's real ``BasePlatformAdapter``.

The message handler below stands in for the gateway runner: it is what ``handle_message`` hands an
admitted event to, and what returns the turn's final reply.
"""

from __future__ import annotations

import asyncio
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
    assert [(o.text, o.next_action) for o in device.outputs] == [("Your timer is done.", "standby")]
    assert device.outputs[0].expires_at > time.time()


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
    assert set(hooks) == {"on_stream_start", "on_stream_delta", "pre_tool_call", "post_api_request"}
    hooks["on_stream_delta"](delta="x", session_id="s")  # no adapter connected: ignored
    adapter = registered["adapter_factory"](PlatformConfig(enabled=True))
    assert isinstance(adapter, ConverseAdapter) and adapter._approval_choice() == "deny"


def test_register_platform_reports_an_unsupporting_hermes():
    def refuse(**kw):
        raise TypeError("unexpected keyword")

    ctx = SimpleNamespace(register_platform=refuse, register_hook=lambda *a: pytest.fail("no hooks"))
    assert register_platform(ctx, ConversationHub()) is False
