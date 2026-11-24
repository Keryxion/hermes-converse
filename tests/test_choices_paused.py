"""A conversation paused on a lost turn, through Hermes's real gateway, authority and SQLite queue,
answered as a voice choice (specs/client.md "Choices", specs/server.md "Choices").

Only agent inference and the answer classifier's model call are replaced. The turn is lost the way
core loses one: its settlement fails, so the drain leaves the admission ``unknown`` and pauses the
conversation's FIFO. ``/discard`` and ``/reset`` run through the gateway's own command handlers.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("gateway.session_authority")


async def _until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class Session:
    """The device session: what the choice reads and sets on it."""

    def __init__(self):
        self.stopped = False
        self.outputs = []
        self.transcripts = SimpleNamespace(put=self.outputs.append)
        self.answer_hook = None
        self.answer_taken = False
        self.playback_until = 0.0
        self.capture_onset = 0.0
        self.is_playing = False
        self._tail_active = False

    def playing(self):
        return self.is_playing


# The answer model's verdicts for this test's phrases; anything else is unclear.
VERDICTS = {"drop it and carry on": "discard", "start fresh": "reset", "never mind": "neither"}


@pytest.fixture
async def paused(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    from gateway import session_results
    from gateway import session_settlement_recovery as recovery
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import admission_row, initialize_session_authority
    from gateway.session_persistence import authority_persistence
    from gateway.session_results import execution_result
    from hermes_converse import approvals, choices
    from hermes_converse.conversation import ConversationHub
    from hermes_converse.platform_adapter import ConverseAdapter, GatewayTurns

    monkeypatch.setattr(choices, "WATCH_SECONDS", 0.05)
    monkeypatch.setattr(recovery, "_SETTLE_RETRY_DELAYS_S", (0.01,), raising=False)
    asked = []

    def aux(system, user, max_tokens):
        assert system == choices.ANSWER_SYSTEM
        asked.append(user)
        reply = user.rsplit("User's reply: ", 1)[1]
        return VERDICTS.get(reply, "unclear")

    monkeypatch.setattr(approvals, "_aux", aux)

    runner = GatewayRunner(GatewayConfig())
    hub = ConversationHub()
    adapter = ConverseAdapter(PlatformConfig(enabled=True), hub, approval_choice=lambda: "deny")
    adapter.gateway_runner = runner
    runner.adapters = {Platform("converse"): adapter}
    authority = await asyncio.wait_for(initialize_session_authority(
        runner, profile_id="default", instance_id="voice-choice"), 10)
    runner._wire_adapter_handlers(adapter)
    await adapter.connect()
    source = adapter.source_for(chat_id="den", title="Arya: Den", recipient_id="den", device_name="Den")
    key = await adapter.bind(source)
    executed = []

    async def agent(event, source, quick_key, run_generation):
        executed.append(event.text)
        captured = execution_result.get()
        if captured is not None:
            captured["result"] = {"final_response": f"Answer to {event.text}", "messages": []}
        return f"Answer to {event.text}"

    monkeypatch.setattr(runner, "_handle_message_with_agent", agent)

    def rows(sid=None):
        sids = [sid] if sid else list(authority.sessions)
        return [admission_row(row) for s in sids for row in
                authority_persistence(authority).admissions.list(session_id=s, pending_only=False)]

    # Lose a turn the way a restart mid-settlement does: the store refuses its result.
    original = session_results.finish_result

    def unavailable(*args, **kwargs):
        raise OSError("database is locked")

    session = Session()
    attachment = hub.attach(source.chat_id, session, "Arya")
    frames, reconnects = [], []
    attachment.send = frames.append
    attachment.reconnect = lambda: reconnects.append(True)
    turns = GatewayTurns(adapter, source, key, settle_timeout=0.05)
    monkeypatch.setattr(session_results, "finish_result", unavailable)
    lost = await asyncio.wait_for(turns.run_turn(text="Turn on the porch light.", on_delta=lambda _t: None), 10)
    monkeypatch.setattr(session_results, "finish_result", original)
    paused_sid = authority.logical_owner(runner.session_store.peek_session_id(key))
    await _until(lambda: any(r["status"] == "unknown" for r in rows(paused_sid)))
    # A request the device sent before this server knew about choices: Hermes saved it behind the pause.
    saved = asyncio.create_task(turns.run_turn(text="What time is it?", on_delta=lambda _t: None))
    await _until(lambda: any(r["status"] == "queued" for r in rows(paused_sid)))
    try:
        yield SimpleNamespace(
            runner=runner, adapter=adapter, authority=authority, hub=hub, source=source, key=key,
            session=session, attachment=attachment, frames=frames, reconnects=reconnects,
            executed=executed, rows=rows, sid=paused_sid, lost=lost, asked=asked, saved=saved,
            choices=adapter.choices, turns=turns)
    finally:
        saved.cancel()
        await asyncio.gather(saved, return_exceptions=True)
        for choice in list(adapter.choices._pending.values()):
            adapter.choices._settle(choice, "cancelled")
        await adapter.cancel_background_tasks()
        await adapter.disconnect()


def statuses(rig):
    return sorted((r["payload"].get("text"), r["status"]) for r in rig.rows(rig.sid))


def said(rig, start):
    """The last output the device was given that begins with *start*."""
    return next((o for o in reversed(rig.session.outputs) if o.text.startswith(start)), None)


def choice_frames(rig, kind):
    return [f for f in rig.frames if f.get("type") == kind]


async def speak(rig, text, *, onset):
    """Captured speech: the session hands it to the answer hook, which takes it (never a request)."""
    rig.session.capture_onset = onset
    assert rig.session.answer_hook is not None and rig.session.answer_hook(text) is True
    await asyncio.sleep(0.05)
    choice = rig.choices.pending(rig.source.chat_id)
    if choice is not None:
        async with choice.lock:
            pass


async def play_out(rig, output):
    """The device played *output* to the end and reported it drained."""
    if output.after_playback is not None:
        await output.after_playback()


async def raised_and_heard(rig, text="Reset the thermostat."):
    before = list(rig.executed)
    assert await rig.choices.before_request(rig.attachment, rig.source, rig.key, text) is True
    assert rig.executed == before, "a request reached the agent while the conversation was paused"
    question = rig.session.outputs[-1]
    await play_out(rig, question)
    return rig.choices.pending(rig.source.chat_id), question


async def test_the_real_pause_notice_is_what_hermes_says(paused):
    """The lost turn really paused the conversation in Hermes, and Hermes said so."""
    assert "/discard" in paused.lost.reply and "/reset" in paused.lost.reply
    assert ("Turn on the porch light.", "unknown") in statuses(paused)
    assert ("What time is it?", "queued") in statuses(paused)
    assert paused.executed == ["Turn on the porch light."]


async def test_speech_before_the_question_is_heard_never_chooses(paused):
    """'Reset the thermostat' is a request, not an answer: it raises the question and nothing else."""
    rows_before = statuses(paused)
    assert await paused.choices.before_request(paused.attachment, paused.source, paused.key,
                                               "Reset the thermostat.") is True
    request = choice_frames(paused, "choice_request")[-1]
    assert [c["name"] for c in request["choices"]] == ["discard", "reset"]
    assert "1 saved message" in request["choices"][0]["description"]
    assert {"type": "tone", "tone": "approval"} in paused.frames
    question = paused.session.outputs[-1]
    assert question.transient and question.next_action == "listen"
    assert "did not send your request" in question.text and question.text.endswith("Say discard or reset.")
    assert statuses(paused) == rows_before, "speech before the question changed the conversation"
    assert paused.asked == [], "speech before the question reached the answer model"

    # Talking over the question while it plays: taken, ignored, the question goes on.
    choice = paused.choices.pending(paused.source.chat_id)
    paused.session.is_playing = True
    await speak(paused, "start fresh", onset=time.monotonic())
    assert len(paused.session.outputs) == 1 and paused.asked == []
    # The barge-in cut the question (no drain, so never heard): the answer is asked for again.
    paused.session.is_playing = False
    await speak(paused, "start fresh", onset=time.monotonic())
    assert len(paused.session.outputs) == 2 and paused.asked == [] and choice.heard_at is None
    assert statuses(paused) == rows_before and not choice_frames(paused, "choice_resolved")


async def test_speech_that_began_at_the_drain_is_not_an_answer(paused):
    choice, _ = await raised_and_heard(paused)
    rows_before, outputs = statuses(paused), len(paused.session.outputs)
    # Began before the drain was reported, or tripped the detector just after it: asked again.
    heard = choice.heard_at
    await speak(paused, "start fresh", onset=heard - 0.3)
    choice.heard_at = heard  # as if that question had drained again
    await speak(paused, "start fresh", onset=heard + 0.1)
    assert paused.asked == [] and statuses(paused) == rows_before
    assert len(paused.session.outputs) == outputs + 2


async def test_off_topic_and_unclear_answers_change_nothing(paused):
    choice, _ = await raised_and_heard(paused)
    rows_before = statuses(paused)
    await speak(paused, "Reset the thermostat.", onset=choice.heard_at + 1)
    assert paused.session.outputs[-1].text.startswith("Sorry, I did not catch that.")
    await play_out(paused, paused.session.outputs[-1])
    await speak(paused, "Hmm, what?", onset=choice.heard_at + 1)
    await play_out(paused, paused.session.outputs[-1])
    assert paused.session.outputs[-1].text == "Nothing changed." and paused.session.outputs[-1].next_action == "standby"
    await speak(paused, "never mind", onset=choice.heard_at + 1)
    assert paused.session.outputs[-1].text == "Okay. Nothing changed."
    assert len(paused.asked) == 3 and statuses(paused) == rows_before
    # The pause holds in Hermes, so the choice does: the next activation answers it.
    assert paused.choices.pending(paused.source.chat_id) is choice
    assert not choice_frames(paused, "choice_resolved")


async def test_spoken_discard_drops_the_lost_turn_without_rerunning_it(paused):
    choice, _ = await raised_and_heard(paused)
    await speak(paused, "drop it and carry on", onset=choice.heard_at + 1)
    await _until(lambda: choice_frames(paused, "choice_resolved"))
    assert choice_frames(paused, "choice_resolved") == [
        {"type": "choice_resolved", "choice_id": choice.choice_id, "outcome": "discard"}]
    await _until(lambda: said(paused, "🗑️ Discarded 1 lost turn"))
    # Hermes's /discard released the request saved behind it; the lost one never ran again.
    await _until(lambda: "What time is it?" in paused.executed)
    assert paused.executed.count("Turn on the porch light.") == 1
    assert not any(r["status"] == "unknown" for r in paused.rows(paused.sid))
    # Settled once: a late tap, a repeated answer and a stale id change nothing.
    paused.choices.respond(paused.attachment, {"type": "choice_response", "choice_id": choice.choice_id,
                                               "choice": "reset"})
    assert paused.session.answer_hook is None
    await asyncio.sleep(0.1)
    assert len(choice_frames(paused, "choice_resolved")) == 1
    assert paused.runner.session_store.peek_session_id(paused.key) == paused.sid
    # The conversation runs again: the next request is an ordinary one.
    assert await paused.choices.before_request(paused.attachment, paused.source, paused.key, "Hi.") is False


async def test_spoken_reset_starts_fresh_and_leaves_the_paused_conversation_as_it_was(paused):
    choice, _ = await raised_and_heard(paused)
    rows_before = statuses(paused)
    await speak(paused, "start fresh", onset=choice.heard_at + 1)
    await _until(lambda: choice_frames(paused, "choice_resolved"))
    assert choice_frames(paused, "choice_resolved")[-1]["outcome"] == "reset"
    fresh = paused.runner.session_store.peek_session_id(paused.key)
    assert fresh and fresh != paused.sid
    # /reset leaves the paused conversation, its lost turn and its saved request, as Hermes keeps them.
    assert statuses(paused) == rows_before
    assert paused.executed == ["Turn on the porch light."]
    await _until(lambda: said(paused, "✨"))
    outcome = said(paused, "✨")
    assert outcome.next_action == "standby" and paused.reconnects == []
    await play_out(paused, outcome)
    assert paused.reconnects == [True], "the device was not sent to the fresh conversation"


async def test_a_tap_answers_once_and_stale_or_invalid_taps_are_ignored(paused):
    assert await paused.choices.before_request(paused.attachment, paused.source, paused.key, "Hello.")
    choice = paused.choices.pending(paused.source.chat_id)
    other = SimpleNamespace(conversation_id="den", session=Session(), send=lambda f: None)
    for frame, attachment in [({"choice_id": "choice-old", "choice": "discard"}, paused.attachment),
                              ({"choice_id": choice.choice_id, "choice": "delete"}, paused.attachment),
                              ({"choice_id": choice.choice_id, "choice": "discard"}, other)]:
        paused.choices.respond(attachment, {"type": "choice_response", **frame})
    await asyncio.sleep(0.1)
    assert not choice_frames(paused, "choice_resolved") and ("Turn on the porch light.", "unknown") in statuses(paused)
    tap = {"type": "choice_response", "choice_id": choice.choice_id, "choice": "discard"}
    paused.choices.respond(paused.attachment, tap)
    paused.choices.respond(paused.attachment, tap)
    await _until(lambda: choice_frames(paused, "choice_resolved"))
    await asyncio.sleep(0.1)
    assert [f["outcome"] for f in choice_frames(paused, "choice_resolved")] == ["discard"]
    assert paused.executed.count("Turn on the porch light.") == 1


async def test_a_reconnect_gets_the_same_choice_and_an_external_discard_cancels_it(paused):
    from gateway.slash_commands_session import discard_lost_turns

    await paused.choices.attached(paused.attachment, paused.source, paused.key)
    first = choice_frames(paused, "choice_request")[-1]
    assert paused.session.outputs == [], "attaching spoke the question"
    # The socket drops and the device comes back: same choice, same id, still unspoken.
    paused.hub.detach("den", paused.attachment)
    session = Session()
    again = paused.hub.attach("den", session, "Arya")
    frames = []
    again.send = frames.append
    await paused.choices.attached(again, paused.source, paused.key)
    assert [f for f in frames if f["type"] == "choice_request"] == [first]
    assert session.outputs == [] and session.answer_hook is not None
    # The same pause read afresh (as after a server restart) derives the same id.
    pause = await paused.choices.pause(paused.key)
    assert pause.choice_id == first["choice_id"]
    # Another chat surface sends /discard first: the choice ends, cancelled, and runs nothing.
    await discard_lost_turns(paused.authority, paused.sid)
    await _until(lambda: any(f["type"] == "choice_resolved" for f in frames))
    assert [f for f in frames if f["type"] == "choice_resolved"] == [
        {"type": "choice_resolved", "choice_id": first["choice_id"], "outcome": "cancelled"}]
    assert session.answer_hook is None and paused.choices.pending("den") is None


async def test_a_choice_revalidates_the_pause_before_running_its_command(paused):
    from gateway.slash_commands_session import discard_lost_turns

    choice, _ = await raised_and_heard(paused)
    ran = []
    original = paused.adapter.run_command

    async def run_command(source, text, **kwargs):
        ran.append(text)
        return await original(source, text, **kwargs)

    paused.adapter.run_command = run_command
    for task in choice.tasks:
        task.cancel()  # the answer comes before the watcher notices
    await discard_lost_turns(paused.authority, paused.sid)
    await speak(paused, "start fresh", onset=choice.heard_at + 1)
    await _until(lambda: choice_frames(paused, "choice_resolved"))
    assert choice_frames(paused, "choice_resolved")[-1]["outcome"] == "cancelled"
    assert ran == [] and paused.runner.session_store.peek_session_id(paused.key) == paused.sid
    await _until(lambda: said(paused, "That is already settled."))
