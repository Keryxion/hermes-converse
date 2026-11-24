"""Which Hermes replies become choices (specs/server.md "Choices"): the whole reply is one of the
blocking notices Hermes itself sends, read from Hermes's own catalog; only that notice's listed
commands are offered, and only those the gateway's command registry runs."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("agent.i18n")

from agent.i18n import t  # noqa: E402

from hermes_converse import approvals, choices  # noqa: E402
from hermes_converse.choices import Choices, notice_family  # noqa: E402
from hermes_converse.conversation import ConversationHub  # noqa: E402

UNAUTHORIZED = ("⏸️ This conversation is paused: a queued message could not be re-authorized (policy_changed). "
                "Your message is saved but will not be answered here. Send /reset to start a fresh conversation, "
                "or ask the operator to resume this one.")


@pytest.mark.parametrize("text, family, names", [
    (t("gateway.errors.context_overflow"), "context_overflow", ["compress", "new"]),
    (t("gateway.errors.history_unavailable"), "history_unavailable", ["reset"]),
    (t("gateway.shared.history_unreadable"), "history_unreadable", ["new"]),
    # The notice names /retry too, only to say it fails the same way: never offered.
    (t("gateway.errors.launch_credentials_unavailable", detail=" (its key was revoked)"), "launch_credentials", ["new"]),
    (UNAUTHORIZED, "unauthorized_queue", ["reset"]),
])
def test_each_blocking_notice_offers_only_its_commands(text, family, names):
    found = notice_family(text)
    assert found is not None and found.name == family
    assert choices.runnable(found.commands) == names


@pytest.mark.parametrize("text", [
    # A model quoting or paraphrasing a notice inside its own reply.
    "Hermes told me: " + t("gateway.errors.context_overflow"),
    t("gateway.errors.context_overflow") + " I can explain what that means.",
    "This conversation is long. Use /compress to shorten the history, or /new to start a fresh conversation.",
    # Help listings, paths, and arbitrary suggested commands.
    "/new — start a new session\n/compress — shorten the history\n/reset — start fresh",
    "The binary is at /usr/bin/reset and the log at /var/log/new.",
    "You could try /model gpt-5 or /resume Project A Plan.",
    # Non-blocking notices: saying the request again continues, so they are not choices.
    t("gateway.errors.generic_failed"),
    t("gateway.errors.rate_limited"),
    # An offer whose command needs Hermes's own argument and a second step: never guessed.
    t("gateway.reset.paused_left", count=2, session_id="20261122_085312_3ac30727", discard="/discard").strip(),
    # The lost-turn pause is read from Hermes's admissions, not from its text.
    ("⏸️ This conversation is paused: a previous turn did not finish when Hermes restarted, and only you "
     "know whether to send it again. Your message is saved. Send /discard to drop the lost turn and continue "
     "(it is not re-run), or /reset to start a fresh conversation."),
    "",
])
def test_text_that_is_not_a_blocking_notice_is_never_a_choice(text):
    assert notice_family(text) is None


def test_a_command_this_gateway_does_not_run_is_not_offered(monkeypatch):
    real = choices.command_catalog.canonical
    monkeypatch.setattr(choices.command_catalog, "canonical", lambda name: None if name == "compress" else real(name))
    assert choices.runnable(notice_family(t("gateway.errors.context_overflow")).commands) == ["new"]


class Session:
    def __init__(self):
        self.stopped = False
        self.outputs = []
        self.transcripts = SimpleNamespace(put=self.outputs.append)
        self.answer_hook = None
        self.answer_taken = False
        self.playback_until = 0.0
        self.capture_onset = 0.0
        self._tail_active = False

    def playing(self):
        return False


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(choices, "WATCH_SECONDS", 0.05)
    monkeypatch.setattr(approvals, "_aux", lambda system, user, n: {
        "shorten it": "compress", "start over": "new"}.get(user.rsplit("User's reply: ", 1)[1], "unclear"))
    hub = ConversationHub()
    sessions = {"key": "s1"}
    ran = []

    async def run_command(source, text, **kwargs):
        ran.append((text, kwargs))
        if text == "/new":
            sessions["key"] = "s2"
            return "✨ Session reset! Starting fresh.\n✦ Tip: ..."
        return "🗜️ Compressed: 40 → 12 messages"

    adapter = SimpleNamespace(_hub=hub, _session_store=SimpleNamespace(peek_session_id=sessions.get),
                              run_command=run_command)
    session = Session()
    attachment = hub.attach("den", session, "Arya")
    frames, reconnects = [], []
    attachment.send = frames.append
    attachment.reconnect = lambda: reconnects.append(True)
    return SimpleNamespace(choices=Choices(adapter), session=session, attachment=attachment, frames=frames,
                           ran=ran, sessions=sessions, reconnects=reconnects)


async def answer(rig, text):
    choice = rig.choices.pending("den")
    await rig.session.outputs[-1].after_playback()
    rig.session.capture_onset = choice.heard_at + 1
    assert rig.session.answer_hook(text) is True
    for _ in range(100):
        await asyncio.sleep(0.01)
        if choice.settled or len(rig.session.outputs) > 1:
            break
    async with choice.lock:
        pass
    return choice


async def test_a_notice_is_asked_after_it_is_spoken_and_runs_the_spoken_command(rig):
    notice = t("gateway.errors.context_overflow")
    assert await rig.choices.after_reply(rig.attachment, None, "key", notice, streamed=False)
    request = rig.frames[0]
    assert request["type"] == "choice_request" and [c["name"] for c in request["choices"]] == ["compress", "new"]
    assert rig.frames[1] == {"type": "tone", "tone": "approval"}
    assert rig.session.outputs[-1].text == "Say compress or new." and rig.session.outputs[-1].next_action == "listen"
    choice = await answer(rig, "shorten it")
    assert rig.ran == [("/compress", {"approved": True})]
    assert rig.frames[-1] == {"type": "choice_resolved", "choice_id": choice.choice_id, "outcome": "compress"}
    assert rig.session.outputs[-1].text == "🗜️ Compressed: 40 → 12 messages" and rig.reconnects == []


async def test_a_fresh_conversation_sends_the_device_to_it_after_the_outcome_plays(rig):
    assert await rig.choices.after_reply(rig.attachment, None, "key", t("gateway.shared.history_unreadable"),
                                         streamed=False)
    assert rig.session.outputs[-1].text == "Say new, or never mind."
    await answer(rig, "start over")
    assert rig.ran[0][0] == "/new" and rig.session.outputs[-1].text == "✨ Session reset! Starting fresh."
    await rig.session.outputs[-1].after_playback()
    assert rig.reconnects == [True]


async def test_streamed_model_text_is_never_a_choice_even_when_it_matches(rig):
    assert not await rig.choices.after_reply(rig.attachment, None, "key", t("gateway.errors.context_overflow"),
                                             streamed=True)
    assert rig.frames == [] and rig.session.outputs == [] and rig.choices.pending("den") is None


async def test_unclear_answers_let_a_notice_choice_go(rig):
    """Only a lost turn blocks the conversation in Hermes; any other choice ends after two unclear
    answers, and the next request goes to Hermes as usual."""
    assert await rig.choices.after_reply(rig.attachment, None, "key", t("gateway.errors.context_overflow"),
                                         streamed=False)
    await answer(rig, "what is the weather")
    assert rig.session.outputs[-1].text.startswith("Sorry, I did not catch that.")
    choice = await answer(rig, "and tomorrow?")
    assert choice.settled and rig.frames[-1]["outcome"] == "cancelled" and rig.ran == []
    assert rig.session.outputs[-1].text == "Nothing changed."
    assert await rig.choices.before_request(rig.attachment, None, "key", "What is the weather?") is False


async def test_a_notice_choice_ends_when_the_conversation_changes_elsewhere(rig):
    assert await rig.choices.after_reply(rig.attachment, None, "key", t("gateway.errors.context_overflow"),
                                         streamed=False)
    choice = rig.choices.pending("den")
    rig.sessions["key"] = "elsewhere"  # another chat surface ran /new on this route
    for _ in range(100):
        await asyncio.sleep(0.01)
        if choice.settled:
            break
    assert rig.frames[-1] == {"type": "choice_resolved", "choice_id": choice.choice_id, "outcome": "cancelled"}
    assert rig.ran == [] and rig.session.answer_hook is None


async def test_a_tap_before_the_question_is_spoken_means_it_is_never_spoken(rig):
    """A screen answers at once: the question still waiting in the device's queue is dropped, so the
    device neither asks an answered question nor opens its microphone for it."""
    import time

    assert await rig.choices.after_reply(rig.attachment, None, "key", t("gateway.errors.context_overflow"),
                                         streamed=False)
    choice, question = rig.choices.pending("den"), rig.session.outputs[-1]
    assert question.expires_at > time.time()
    rig.choices.respond(rig.attachment, {"type": "choice_response", "choice_id": choice.choice_id, "choice": "new"})
    for _ in range(100):
        await asyncio.sleep(0.01)
        if choice.settled and rig.session.outputs[-1] is not question:
            break
    assert rig.ran[0][0] == "/new" and rig.frames[-1]["outcome"] == "new"
    assert question.expires_at <= time.time(), "the answered question would still be spoken"
    assert rig.session.outputs[-1].text == "✨ Session reset! Starting fresh."


async def test_asking_again_never_queues_two_questions(rig):
    import time

    assert await rig.choices.after_reply(rig.attachment, None, "key", t("gateway.errors.context_overflow"),
                                         streamed=False)
    first = rig.session.outputs[-1]
    rig.choices._ask(rig.choices.pending("den"), rig.attachment)
    assert first.expires_at <= time.time() and rig.session.outputs[-1].expires_at > time.time()
    assert [f["type"] for f in rig.frames].count("choice_request") == 1
