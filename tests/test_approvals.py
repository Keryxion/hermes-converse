"""Voice approvals (specs/server.md, "Approvals"): the summary says who is asking."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("tools.approval")

from hermes_converse import approvals  # noqa: E402
from hermes_converse.approvals import Approvals, phrase  # noqa: E402
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
    hub = ConversationHub()
    adapter = SimpleNamespace(_hub=hub, _turns={})
    return SimpleNamespace(hub=hub, approvals=Approvals(adapter), resolved=resolved)


def attach(rig, name="Arya"):
    session = Session()
    attachment = rig.hub.attach("den", session, name)
    frames = []
    attachment.send = frames.append
    return session, frames


PROMPT = SimpleNamespace(request_id="r1", session_key="k", chat_id="den", command="nc 10.0.4.17 6053",
                         description="Network connection (to a device)", choices=["once", "deny"])


async def asked(rig, frames):
    await rig.approvals.begin(PROMPT)
    for _ in range(100):
        if frames:
            break
        await asyncio.sleep(0.01)
    return frames[0]


@pytest.mark.parametrize("text, expected", [
    ("Start a new session (fresh session ID + history)", "start a new session, with fresh session ID + history"),
    ("Compress conversation context.", "compress conversation context"),
    ("SSH into the host!", "SSH into the host"),
    ("  open the door  ", "open the door"),
    ("delete files (recursive).", "delete files, with recursive"),
])
def test_phrase_makes_a_description_speakable(text, expected):
    assert phrase(text) == expected


async def test_summary_names_the_assistant_and_asks_okay(rig, monkeypatch):
    seen = []
    monkeypatch.setattr(approvals, "_aux", lambda system, user, n: seen.append((system, user)) or
                        "Open a network connection to the Kitchen speaker to read its logs.")
    session, frames = attach(rig)
    request = await asked(rig, frames)
    summary = "Arya wants to open a network connection to the Kitchen speaker to read its logs. Okay?"
    assert request["type"] == "approval_request" and request["summary"] == summary
    assert frames[1] == {"type": "tone", "tone": "approval"}
    assert [o.text for o in session.outputs] == [summary + " Say yes or no."]
    assert "wants to" in seen[0][0] and "nc 10.0.4.17 6053" in seen[0][1]
    rig.approvals._settle(rig.approvals._pending["den"], "deny", "denied")


async def test_a_failed_summary_call_says_hermess_description(rig, monkeypatch):
    def fail(*_):
        raise RuntimeError("no auxiliary model")

    monkeypatch.setattr(approvals, "_aux", fail)
    _session, frames = attach(rig, name="Hal")
    request = await asked(rig, frames)
    assert request["summary"] == "Hal wants to network connection, with to a device. Okay?"
    rig.approvals._settle(rig.approvals._pending["den"], "deny", "denied")


async def test_no_assistant_name_and_the_re_ask(rig, monkeypatch):
    replies = iter(["run the tests", "pardon"])
    monkeypatch.setattr(approvals, "_aux", lambda *_: next(replies))
    session, frames = attach(rig, name="")
    request = await asked(rig, frames)
    assert request["summary"] == "The assistant wants to run the tests. Okay?"
    pending = rig.approvals._pending["den"]
    await rig.approvals._answer(pending, "hmm")
    assert session.outputs[-1].text == ("Sorry, I did not catch that. The assistant wants to run the tests. Okay? "
                                        "Say yes or no.")
    rig.approvals._settle(pending, "deny", "denied")
