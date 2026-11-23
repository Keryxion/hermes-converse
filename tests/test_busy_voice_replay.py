"""Reproduce #133 through Hermes's real busy admission and SQLite queue.

Only agent inference is replaced. Events hold the first turn open until the voice
input is durably queued, eliminating the live /model timing race.
"""
from __future__ import annotations

import asyncio
from queue import Queue
from types import SimpleNamespace

import pytest


async def _until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("busy,cancelled", [(False, False), (True, False), (True, True)],
                         ids=["idle-control", "busy-replay", "cancel-queued"])
@pytest.mark.parametrize("reply,streamed", [("Four.", True), ("Four.", False), (None, False),
                                           ("[SILENT]", False)],
                         ids=["streamed", "unstreamed", "empty", "silent"])
async def test_voice_reply_survives_real_gateway_admission(
        tmp_path, monkeypatch, busy, cancelled, reply, streamed):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_results import execution_result
    from gateway.session_authority import admission_row
    from gateway.session_persistence import authority_persistence
    from hermes_converse.conversation import ConversationHub
    from hermes_converse.platform_adapter import ConverseAdapter, GatewayTurns

    runner = GatewayRunner(GatewayConfig())
    hub = ConversationHub()
    adapter = ConverseAdapter(PlatformConfig(enabled=True), hub, approval_choice=lambda: "deny")
    adapter.gateway_runner = runner
    runner.adapters = {Platform("converse"): adapter}
    authority = await asyncio.wait_for(initialize_session_authority(
        runner, profile_id="default", instance_id="voice-repro"), 10)
    runner._wire_adapter_handlers(adapter)
    await adapter.connect()
    source = adapter.source_for(chat_id="repro", title="Arya: Repro", recipient_id="repro",
                                device_name="Repro")
    key = await adapter.bind(source)
    # Keep a device attached as in production. Proactive output cannot finish its
    # pending voice turn, even if the hub accepts the misclassified reply.
    hub.attach(source.chat_id, SimpleNamespace(stopped=False, transcripts=Queue()))
    turns = GatewayTurns(adapter, source, key, settle_timeout=0.05)
    entered, release, answered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    executed = []
    starts, completions = [], []
    start, complete = adapter.on_processing_start, adapter.on_processing_complete

    async def on_start(event):
        starts.append(event.text)
        await start(event)

    async def on_complete(event, outcome):
        completions.append(event.text)
        await complete(event, outcome)

    monkeypatch.setattr(adapter, "on_processing_start", on_start)
    monkeypatch.setattr(adapter, "on_processing_complete", on_complete)

    async def agent(event, source, quick_key, run_generation):
        executed.append(event)
        if event.text == "occupy the conversation":
            entered.set()
            await release.wait()
            final = "First turn finished."
        else:
            final = reply
            if streamed:
                sid = runner.session_store.peek_session_id(key)
                adapter.on_stream_start(session_id=sid)
                adapter.on_stream_delta(delta=reply, session_id=sid)
            answered.set()
        captured = execution_result.get()
        if captured is not None:
            captured["result"] = {"final_response": final or "", "messages": []}
        return final

    monkeypatch.setattr(runner, "_handle_message_with_agent", agent)

    def admissions():
        return [admission_row(row) for sid in authority.sessions for row in
                authority_persistence(authority).admissions.list(session_id=sid, pending_only=False)]

    task = None
    spoken = []
    try:
        if busy:
            await adapter.handle_message(MessageEvent(text="occupy the conversation", source=source,
                                                     message_id="first", allow_gateway_control=False))
            await asyncio.wait_for(entered.wait(), 5)
        task = asyncio.create_task(turns.run_turn(text="What is two plus two?", on_delta=spoken.append))
        await _until(lambda: turns._turn is not None)
        original_id = turns._turn.event.message_id
        if busy:
            await _until(lambda: any(row["payload"]["text"] == "What is two plus two?"
                                    and row["status"] == "queued" for row in admissions()))
            assert not answered.is_set(), "the queued turn ran before the first turn was released"
            if cancelled:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not adapter.has_pending_interrupt(key), "cancel interrupted the preceding turn"
            release.set()
        await asyncio.wait_for(answered.wait(), 5)
        await _until(lambda: not adapter._background_tasks)
        replay = next(event for event in executed if event.text == "What is two plus two?")
        print(f"busy={busy} original={original_id} executed={replay.message_id} "
              f"spoken={spoken!r} voice_done={turns._turn.done.is_set()} "
              f"starts={starts!r} completions={completions!r}")
        if busy:
            assert replay is not turns._turn.event, "the test did not exercise durable replay"
            assert any(row["payload"]["text"] == replay.text and row["status"] == "terminal"
                       for row in admissions()), "the gateway did not finish the queued input"
        expected = [reply] if reply and reply != "[SILENT]" and not cancelled else []
        assert spoken == expected, "gateway answered, but the voice callback received no reply"
        if not cancelled:
            result = await asyncio.wait_for(asyncio.shield(task), 1)
            assert result.status == "completed" and result.reply == (reply or "")
        assert turns.settled and not adapter._abandoned
        # The queued voice reply must not be delivered a second time as proactive output.
        assert hub.attachment(source.chat_id).session.transcripts.qsize() == int(busy)
        followup = []
        result = await asyncio.wait_for(turns.run_turn(text="Ask again.", on_delta=followup.append), 5)
        expected_followup = [reply] if reply and reply != "[SILENT]" else []
        assert result.status == "completed" and followup == expected_followup and turns.settled
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await adapter.cancel_background_tasks()
        await adapter.disconnect()
