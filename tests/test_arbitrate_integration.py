"""Integration test for arbitration wired through ``drive_converse_turns``.

This needs only stdlib + numpy: the ``speech`` seams the turn driver touches (thread spawning,
sentence chunking, markdown stripping) are replaced in-process, and the session's transcript queue
is fed directly. That lets us verify the real driver path — (a) only the winning device runs the
agent turn, (b) the loser stays silent, and (c) release allows re-election. Run with::

    python3 -m pytest tests/test_arbitrate_integration.py -q
"""
from __future__ import annotations

import asyncio
import re
import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pytest

import hermes_converse.loop as cl
from hermes_converse import speech
from hermes_converse.arbitrate import Arbitrator, LocalBus
from hermes_converse.loop import ConverseSession, drive_converse_turns, Utterance


@pytest.fixture(autouse=True)
def _speech_seams(monkeypatch):
    """Replace the Hermes speech seams the driver uses with in-process equivalents."""

    class _Chunker:
        def __init__(self) -> None:
            self.buf = ""

        def feed(self, delta: str):
            self.buf += delta
            out = []
            while True:
                cuts = [i for i in (self.buf.find("."), self.buf.find("!"), self.buf.find("?")) if i >= 0]
                if not cuts:
                    break
                cut = min(cuts)
                out.append(self.buf[: cut + 1])
                self.buf = self.buf[cut + 1:]
            return out

        def flush(self):
            out = [self.buf] if self.buf.strip() else []
            self.buf = ""
            return out

    def _spawn(target, *, name, args=()):
        return threading.Thread(target=target, name=name, args=args, daemon=True)

    monkeypatch.setattr(speech, "spawn_context_thread", _spawn)
    monkeypatch.setattr(speech, "sentence_chunker", lambda: _Chunker())
    monkeypatch.setattr(speech, "strip_markdown", lambda text: text)
    monkeypatch.setattr(speech, "sentence_boundary_re", lambda: re.compile(r"(?<=[.!?])\s+"))


class _FakeSynth:
    sample_rate = 24000

    def __init__(self) -> None:
        self.spoken: list[str] = []

    def synth(self, text: str):
        self.spoken.append(text)
        return iter([])  # no PCM


@dataclass
class _RunCall:
    transcript: str
    interrupted: bool


class _Harness:
    def __init__(self, *, arbitrator=None, device=None, device_id="d1", hold=None):
        self.arbitrator = arbitrator
        self.device = device
        self.device_id = device_id
        self.hold = hold
        self.synth = _FakeSynth()
        self.run_calls: list[_RunCall] = []
        self.sent: list[dict] = []
        self.owned_utterance: Optional[str] = None
        self.session = ConverseSession(np, transcribe=lambda p: "")
        self.loop = asyncio.get_running_loop()

    async def run(self, transcripts):
        async def run_turn(transcript, on_delta, *, interrupted):
            self.run_calls.append(_RunCall(transcript, interrupted))
            if self.hold is not None:
                await self.hold.wait()  # keep the turn open so a loser's claim still lands
            return f"reply to {transcript}", None

        sent_json = self.sent

        async def send_json(msg):
            sent_json.append(msg)

        async def send_bytes(chunk):
            pass

        owned_getter = lambda: self.owned_utterance
        owned_setter = lambda v: self.__setattr__("owned_utterance", v)

        await drive_converse_turns(
            session=self.session,
            synth=self.synth,
            cap=4000,
            loop=self.loop,
            send_json=send_json,
            send_bytes=send_bytes,
            run_turn=run_turn,
            history=[],
            arbitrator=self.arbitrator,
            device=self.device,
            device_id=self.device_id,
            owned_utterance_getter=owned_getter,
            owned_utterance_setter=owned_setter,
        )

    def feed(self, text):
        self.session.transcripts.put(Utterance(text, None))


# ── tests ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_without_arbitration_every_device_runs_its_turn():
    harness = _Harness()
    task = asyncio.create_task(harness.run(["hello"]))
    harness.feed("hello")
    await asyncio.sleep(0.3)
    harness.session.stop()
    await task
    assert len(harness.run_calls) == 1
    assert harness.run_calls[0].transcript == "hello"


@pytest.mark.asyncio
async def test_with_arbitration_winner_runs_loser_silent():
    arb = Arbitrator(bus=LocalBus(), policy="first_to_endpoint")
    d1 = await arb.register("d1")
    d2 = await arb.register("d2")

    # Keep d1's turn open until both devices have claimed: in the real world a turn takes
    # seconds (LLM + TTS), so the loser's claim always lands while the winner owns the utterance.
    hold = asyncio.Event()
    h1 = _Harness(arbitrator=arb, device=d1, device_id="d1", hold=hold)
    h2 = _Harness(arbitrator=arb, device=d2, device_id="d2", hold=hold)

    t1 = asyncio.create_task(h1.run(["hello"]))
    t2 = asyncio.create_task(h2.run(["hello"]))

    # Both devices hear the same utterance. d1 claims first and wins.
    h1.feed("hello")
    await asyncio.sleep(0.05)  # d1 claimed and is now holding the turn open
    h2.feed("hello")
    await asyncio.sleep(0.05)  # d2 claims while d1 still owns the utterance -> denied
    hold.set()
    await asyncio.sleep(0.1)

    h1.session.stop()
    h2.session.stop()
    await t1
    await t2

    # Only d1 ran the agent turn.
    assert len(h1.run_calls) == 1
    assert h1.run_calls[0].transcript == "hello"
    assert len(h2.run_calls) == 0
    # d2 stayed silent: no synthesis.
    assert h2.synth.spoken == []


@pytest.mark.asyncio
async def test_winner_release_allows_re_election():
    arb = Arbitrator(bus=LocalBus(), policy="first_to_endpoint")
    d1 = await arb.register("d1")

    harness = _Harness(arbitrator=arb, device=d1, device_id="d1")
    task = asyncio.create_task(harness.run(["one", "two"]))
    harness.feed("one")
    await asyncio.sleep(0.4)
    harness.feed("two")
    await asyncio.sleep(0.5)
    harness.session.stop()
    await task

    # Both utterances were claimed and won by d1 (re-elected after release).
    assert len(harness.run_calls) == 2
    assert [c.transcript for c in harness.run_calls] == ["one", "two"]
