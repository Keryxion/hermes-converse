"""Choices over a real socket (specs/client.md "Choices"): the route, its session's capture and drain,
and the converse adapter, over the fake gateway. The lost-turn pause itself is read from Hermes's
authority in tests/test_choices_paused.py; here that read is stubbed."""

from __future__ import annotations

import asyncio

import aiohttp

from hermes_converse import approvals, choices, speech
from hermes_converse.named import DeviceStore
from tests.test_gateway_route import _connect, _say
from tests.test_arbitrate_integration import _patch
from tests.test_route import Harness, _collect_turn, _speech_then_silence_pcm

DISCARDED = "🗑️ Discarded 1 lost turn(s); they were not re-run. Your saved messages will be answered now."


def _rig(monkeypatch, heard):
    fake = _patch(monkeypatch)
    fake.command_replies["discard"] = DISCARDED
    state = {"paused": True}

    async def pause(self, session_key):
        return choices.Pause("s1", (("lost-1", 4),), 1) if state["paused"] else None

    monkeypatch.setattr(choices.Choices, "pause", pause)
    monkeypatch.setattr(choices, "WATCH_SECONDS", 0.05)
    monkeypatch.setattr(speech, "transcribe_wav", lambda *a, **kw: heard.pop(0) if heard else "hello")
    monkeypatch.setattr(approvals, "_aux", lambda system, user, n: {
        "drop it": "discard", "start fresh": "reset"}.get(user.rsplit("User's reply: ", 1)[1], "unclear"))
    return fake, state


async def _frames_until(ws, kind, timeout=5.0):
    frames = []
    async with asyncio.timeout(timeout):
        while True:
            message = await ws.receive()
            if message.type != aiohttp.WSMsgType.TEXT:
                continue  # reply audio
            frame = message.json()
            frames.append(frame)
            if frame["type"] == kind:
                return frames


async def test_a_paused_conversation_is_answered_by_voice_and_never_sent_a_request(tmp_path, monkeypatch):
    heard = ["what time is it", "drop it", "drop it"]
    fake, state = _rig(monkeypatch, heard)
    async with Harness(fake, device_store=DeviceStore(tmp_path / "devices.db")) as client:
        ws, ready = await _connect(client)
        # Attaching to a paused conversation sends the choice and says nothing.
        request = await ws.receive_json(timeout=3)
        assert request["type"] == "choice_request"
        assert [c["name"] for c in request["choices"]] == ["discard", "reset"]
        # The next activation's request is not sent: the question is asked instead.
        for chunk in _speech_then_silence_pcm():
            await ws.send_bytes(chunk)
        frames = await _frames_until(ws, "speaking")
        assert {"type": "tone", "tone": "approval"} in frames
        assert not any(f["type"] == "choice_request" for f in frames), "the same device got the choice twice"
        rest, pcm = await _collect_turn(ws)  # drains: the device reports `listening`
        assert pcm and rest[-1]["next_action"] == "listen"
        assert not any(f["type"] == "transcript" for f in frames + rest)
        assert fake.turns == [], "a request reached Hermes while the conversation was paused"
        # Speech that begins the moment the drain is reported is not an answer: asked again.
        frames, pcm = await _say(ws)
        assert {"type": "tone", "tone": "approval"} in frames and frames[-1]["next_action"] == "listen"
        assert not any(f["type"] == "choice_resolved" for f in frames)
        await asyncio.sleep(0.6)
        # Spoken after the question drained: the answer runs /discard as the chat's own command.
        frames, pcm = await _say(ws)
        resolved = [f for f in frames if f["type"] == "choice_resolved"]
        assert resolved == [{"type": "choice_resolved", "choice_id": request["choice_id"], "outcome": "discard"}]
        assert frames[-1]["type"] == "turn_done" and frames[-1]["next_action"] == "standby" and pcm
        assert fake.turns == []
        state["paused"] = False
        await ws.close()


async def test_a_fresh_conversation_reconnects_the_device_after_the_outcome(tmp_path, monkeypatch):
    heard = ["what time is it", "start fresh"]
    fake, state = _rig(monkeypatch, heard)
    async with Harness(fake, device_store=DeviceStore(tmp_path / "devices.db")) as client:
        ws, _ = await _connect(client)
        assert (await ws.receive_json(timeout=3))["type"] == "choice_request"
        await _say(ws)  # asked
        await asyncio.sleep(0.6)
        frames, _ = await _say(ws)
        assert any(f.get("outcome") == "reset" for f in frames)
        state["paused"] = False
        message = await ws.receive(timeout=3)
        assert ws.close_code == 4410, message
    assert [t["text"] for t in fake.turns] == ["/reset"], "only the chosen command reached Hermes"


async def test_a_screen_tap_answers_and_a_repeated_tap_is_ignored(tmp_path, monkeypatch):
    fake, state = _rig(monkeypatch, [])
    async with Harness(fake, device_store=DeviceStore(tmp_path / "devices.db")) as client:
        ws, _ = await _connect(client)
        request = await ws.receive_json(timeout=3)
        tap = {"type": "choice_response", "choice_id": request["choice_id"], "choice": "discard"}
        await ws.send_json({**tap, "choice_id": "choice-stale"})
        await ws.send_json(tap)
        await ws.send_json(tap)
        frames = await _frames_until(ws, "turn_done")
        assert [f["outcome"] for f in frames if f["type"] == "choice_resolved"] == ["discard"]
        state["paused"] = False
        await ws.close()
    assert fake.turns == []  # the fake answers /discard itself (command_replies) and records no turn
