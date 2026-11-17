"""Voice approvals: direct descriptions, named questions, and spoken outcomes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("tools.approval")

from hermes_converse import approvals  # noqa: E402
from hermes_converse.approvals import Approvals  # noqa: E402
from hermes_converse.conversation import ConversationHub  # noqa: E402


class Session:
    def __init__(self):
        self.stopped = False
        self.outputs = []
        self.transcripts = SimpleNamespace(put=self.outputs.append)
        self.answer_hook = None


@pytest.fixture
def rig(monkeypatch):
    resolved = []
    monkeypatch.setattr("tools.approval.resolve_gateway_approval",
                        lambda key, choice, request_id=None: resolved.append(choice) or 1)
    monkeypatch.setattr("tools.approval.list_gateway_approvals", lambda key: [{"request_id": "r1"}])

    def no_model(*_):
        pytest.fail("Asking permission must not call a model")

    monkeypatch.setattr(approvals, "_aux", no_model)
    hub = ConversationHub()
    adapter = SimpleNamespace(_hub=hub, _turns={})
    return SimpleNamespace(hub=hub, adapter=adapter, approvals=Approvals(adapter), resolved=resolved)


def attach(rig, name="Arya"):
    session = Session()
    attachment = rig.hub.attach("den", session, name)
    frames = []
    attachment.send = frames.append
    return session, frames


PROMPT = SimpleNamespace(request_id="r1", session_key="k", chat_id="den", command="nc 10.0.4.17 6053",
                         description="Network connection (to a device)", choices=["once", "deny"])


async def test_summary_uses_description_without_a_model(rig):
    session, frames = attach(rig)
    await rig.approvals.begin(PROMPT)
    summary = "Arya wants permission for: Network connection (to a device). Okay?"
    assert frames[0]["type"] == "approval_request" and frames[0]["summary"] == summary
    assert frames[1] == {"type": "tone", "tone": "approval"}
    assert [o.text for o in session.outputs] == [summary + " Say yes or no."]
    rig.approvals.respond(rig.hub.attachment("den"), {"approval_id": "r1", "choice": "deny"})


async def test_no_assistant_name_and_the_re_ask(rig, monkeypatch):
    session, frames = attach(rig, name="")
    await rig.approvals.begin(PROMPT)
    summary = "The assistant wants permission for: Network connection (to a device). Okay?"
    assert frames[0]["summary"] == summary
    pending = rig.approvals._pending["den"]
    monkeypatch.setattr(approvals, "_aux", lambda *_: "unclear")
    await rig.approvals._answer(pending, "hmm")
    assert session.outputs[-1].text == "Sorry, I did not catch that. " + summary + " Say yes or no."
    await rig.approvals._answer(pending, "hmm")
    assert session.outputs[-1].text == "Denied."
    assert rig.resolved == ["deny"]


async def test_empty_description(rig):
    session, frames = attach(rig)
    await rig.approvals.begin(SimpleNamespace(**{**vars(PROMPT), "description": "  "}))
    assert frames[0]["summary"] == "Arya wants permission for: Run a command. Okay?"
    rig.approvals.respond(rig.hub.attachment("den"), {"approval_id": "r1", "choice": "deny"})


@pytest.mark.parametrize("initially_attached", [False, True])
async def test_held_approval_uses_name_on_attach(rig, initially_attached):
    if initially_attached:
        attach(rig)
    await rig.approvals.begin(PROMPT)
    original = rig.hub.attachment("den")
    if original is not None:
        rig.hub.detach("den", original)
        assert rig.approvals.on_stop("den")
    session, frames = attach(rig)
    rig.approvals.attached(rig.hub.attachment("den"))
    assert frames[0]["summary"] == "Arya wants permission for: Network connection (to a device). Okay?"
    assert session.outputs[0].text == frames[0]["summary"] + " Say yes or no."
    rig.approvals.respond(rig.hub.attachment("den"), {"approval_id": "r1", "choice": "deny"})


@pytest.mark.parametrize("outcome", ["approved", "denied", "expired", "cancelled"])
async def test_outcome_is_spoken_and_sent_once(rig, monkeypatch, outcome):
    session, frames = attach(rig)
    await rig.approvals.begin(PROMPT)
    attachment = rig.hub.attachment("den")
    if outcome in ("approved", "denied"):
        response = {"approval_id": "r1", "choice": "once" if outcome == "approved" else "deny"}
        rig.approvals.respond(attachment, response)
        rig.approvals.respond(attachment, response)
    elif outcome == "expired":
        monkeypatch.setattr("tools.approval.list_gateway_approvals", lambda key: [])
        monkeypatch.setattr(approvals, "WATCH_SECONDS", 0)
        await rig.approvals._watch(rig.approvals._pending["den"])
    else:
        assert not rig.approvals.on_stop("den")
        assert not rig.approvals.on_stop("den")
    assert [o.text for o in session.outputs][1:] == [outcome.capitalize() + "."]
    assert frames[2:] == [{"type": "approval_resolved", "approval_id": "r1", "outcome": outcome}]
    assert rig.resolved == ([] if outcome == "expired" else ["once" if outcome == "approved" else "deny"])
    assert session.answer_hook is None


@pytest.mark.parametrize("outcome", ["approved", "cancelled"])
async def test_outcome_during_a_running_turn(rig, outcome):
    session, frames = attach(rig)
    deltas = []
    rig.adapter._turns["den"] = SimpleNamespace(started=True, outcome=None, stopped=False,
                                               detached=False, on_delta=deltas.append)
    await rig.approvals.begin(PROMPT)
    if outcome == "approved":
        rig.approvals.respond(rig.hub.attachment("den"), {"approval_id": "r1", "choice": "once"})
        assert deltas[-1] == "Approved.\n"
        assert session.outputs == []
    else:
        rig.approvals.on_stop("den")
        # Cancellation discards the running turn's audio; the confirmation is queued separately.
        assert len(deltas) == 1
        assert [o.text for o in session.outputs] == ["Cancelled."]


async def test_spoken_yes_confirms_approval(rig, monkeypatch):
    session, frames = attach(rig)
    await rig.approvals.begin(PROMPT)
    monkeypatch.setattr(approvals, "_aux", lambda *_: "once")
    await rig.approvals._answer(rig.approvals._pending["den"], "yes")
    assert rig.resolved == ["once"]
    assert session.outputs[-1].text == "Approved."


@pytest.mark.parametrize("answer", ["frame", "spoken", "spoken_late"])
async def test_command_answers_after_the_deadline_cannot_authorize(rig, monkeypatch, answer):
    import time

    _session, frames = attach(rig)
    asking = asyncio.create_task(rig.approvals.ask("den", "/new", "Start over?", 30))
    await asyncio.sleep(0)
    pending = rig.approvals._pending["den"]
    if answer != "spoken_late":
        pending.deadline = time.monotonic() - 1  # the watcher has not observed expiry yet
    if answer == "frame":
        rig.approvals.respond(rig.hub.attachment("den"), {
            "approval_id": pending.request_id, "choice": "once"})
    else:
        def classify(*_):
            pending.deadline = time.monotonic() - 1  # expiry while classifying the spoken answer
            return "once"
        monkeypatch.setattr(approvals, "_aux", classify)
        await rig.approvals._answer(pending, "yes")
    assert await asking is None
    assert frames[-1]["outcome"] == "expired" and rig.resolved == []


async def test_command_approval_expires_without_polling(rig, monkeypatch):
    monkeypatch.setattr(approvals, "WATCH_SECONDS", 60)
    session, frames = attach(rig)
    assert await rig.approvals.ask("den", "/new", "Start over?", .01) is None
    assert frames[-1]["outcome"] == "expired"
    assert session.answer_hook is None and rig.resolved == []


async def test_command_question_can_be_held_and_answers_never_go_to_hermes(rig):
    asking = asyncio.create_task(rig.approvals.ask("den", "/new", "Start over?", 30))
    await asyncio.sleep(0)
    session, frames = attach(rig)
    rig.approvals.attached(rig.hub.attachment("den"))
    request = frames[0]
    assert request["choices"] == ["once", "deny"]
    assert [o.text for o in session.outputs] == ["Start over? Say yes or no."]
    rig.approvals.respond(rig.hub.attachment("den"), {"approval_id": request["approval_id"], "choice": "once"})
    assert await asking == "once" and rig.resolved == []
