"""Integration test for arbitration wired through ``drive_converse_turns``.

This does not need the Hermes ``agent`` module: it patches ``speech`` at the seam and feeds the
session's transcript queue directly, so we can verify that (a) only the winning device runs the
agent turn and (b) the loser stays silent. Run with::

    python3 -m pytest tests/test_arbitrate_integration.py -q
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pytest

import hermes_converse.loop as cl
from hermes_converse.arbitrate import Arbitrator, LocalBus
from hermes_converse.loop import ConverseSession, drive_converse_turns, Utterance


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
    def __init__(self, *, arbitrator=None, device=None, device_id="d1"):
        self.arbitrator = arbitrator
        self.device = device
        self.device_id = device_id
        self.synth = _FakeSynth()
        self.run_calls: list[_RunCall] = []
        self.sent: list[dict] = []
        self.owned_utterance: Optional[str] = None
        self.session = ConverseSession(np, transcribe=lambda p: "")
        self.loop = asyncio.get_running_loop()

    async def run(self, transcripts):
        async def run_turn(transcript, on_delta, *, interrupted):
            self.run_calls.append(_RunCall(transcript, interrupted))
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

    h1 = _Harness(arbitrator=arb, device=d1, device_id="d1")
    h2 = _Harness(arbitrator=arb, device=d2, device_id="d2")

    t1 = asyncio.create_task(h1.run(["hello"]))
    t2 = asyncio.create_task(h2.run(["hello"]))

    # Both devices hear the same utterance. d1 claims first and wins.
    h1.feed("hello")
    await asyncio.sleep(0.05)
    h2.feed("hello")
    await asyncio.sleep(0.5)

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
