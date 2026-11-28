"""Keep execution, device tools and channel delivery independently observable."""
import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("gateway.platforms.base")

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from hermes_converse.conversation import ConversationHub
from hermes_converse.platform_adapter import ConverseAdapter
from hermes_converse.voice_tools import make_handlers
from tests.support.fake_hermes import FakeStore


@pytest.mark.parametrize("disconnect_during_work", [False, True])
async def test_detached_gateway_work_finishes_without_channel_failure(monkeypatch, tmp_path,
                                                                      disconnect_during_work):
    from gateway import session_context

    hub = ConversationHub()
    adapter = ConverseAdapter(PlatformConfig(enabled=True), hub, approval_choice=lambda: "deny")
    adapter.set_session_store(FakeStore())
    await adapter.connect()
    source = adapter.source_for(chat_id="den", title="Arya: Den Echo", recipient_id="r",
                                device_name="Den Echo")
    monkeypatch.setattr(session_context, "get_session_env", lambda key, default="": {
        "HERMES_SESSION_PLATFORM": "converse", "HERMES_SESSION_CHAT_ID": "den"
    }.get(key, default))
    outputs = []
    attachment = None
    if disconnect_during_work:
        device = SimpleNamespace(stopped=False, transcripts=SimpleNamespace(put=outputs.append))
        attachment = hub.attach("den", device)
    entered, proceed, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    receipts, outcomes = [], []
    artifact = tmp_path / "work-completed.txt"
    original_send = adapter.send
    original_complete = adapter.on_processing_complete

    async def send(chat_id, content, **kwargs):
        result = await original_send(chat_id, content, **kwargs)
        receipts.append((content, result))
        return result

    async def complete(event, outcome):
        outcomes.append(outcome.value)
        await original_complete(event, outcome)
        completed.set()

    async def handler(event):
        entered.set()
        await proceed.wait()
        # A device tool refusal is a result, not an execution exception.
        tool_result = json.loads(make_handlers(hub)["speak_this"]({"text": "Progress."}))
        artifact.write_text("finished after " + tool_result["result"])
        return "Background work completed."

    monkeypatch.setattr(adapter, "send", send)
    monkeypatch.setattr(adapter, "on_processing_complete", complete)
    adapter.set_message_handler(handler)
    try:
        event = MessageEvent(text="[background work]", message_type=MessageType.TEXT,
                             source=source, message_id="detached-work", internal=True)
        await adapter.handle_message(event)
        await asyncio.wait_for(entered.wait(), 2)
        if attachment is not None:
            hub.detach("den", attachment)
        proceed.set()
        await asyncio.wait_for(completed.wait(), 5)
        assert artifact.read_text() == "finished after not_attached"
        assert outputs == []
        assert len(receipts) == 1
        text, receipt = receipts[0]
        assert text == "Background work completed."
        assert receipt.success and receipt.error is None
        assert outcomes == ["success"]
        # Nothing is queued for a replacement transport.
        replacement = SimpleNamespace(stopped=False, transcripts=SimpleNamespace(put=outputs.append))
        hub.attach("den", replacement)
        assert outputs == []
    finally:
        await adapter.cancel_background_tasks()
        await adapter.disconnect()
