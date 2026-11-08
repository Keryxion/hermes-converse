"""Cross-device voice arbitration (``hermes_converse.arbitrate``).

These tests need only the standard library: the arbitrator has no audio, VAD, STT, TTS or Hermes
imports. Run with::

    python3 -m pytest tests/test_arbitrate.py
"""
from __future__ import annotations

import asyncio
import pytest

from hermes_converse.arbitrate import (
    Arbitrator, Device, DeviceAnnounce, LocalBus, TurnClaim, TurnDenied, TurnGranted,
    TurnReleased, POLICY_FIRST_TO_ENDPOINT, POLICY_PROXIMITY,
)


# ── helpers ──────────────────────────────────────────────────────────────────────────────────

async def _gather(arbitrator, claims):
    """Run several claim_turn calls concurrently and return their (granted, winner) results."""
    return await asyncio.gather(*[
        arbitrator.claim_turn(did, utt, f"transcript {i}")
        for i, (did, utt) in enumerate(claims)
    ])


# ── bus ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_local_bus_fan_out():
    bus = LocalBus()
    q1 = await bus.subscribe()
    q2 = await bus.subscribe()
    await bus.publish(TurnClaim("d1", "u1", "hi", None, None, 0.0))
    msgs = [await asyncio.wait_for(q1.get(), timeout=1),
            await asyncio.wait_for(q2.get(), timeout=1)]
    assert all(isinstance(m, TurnClaim) for m in msgs)


@pytest.mark.asyncio
async def test_local_bus_unsubscribe_stops_delivery():
    bus = LocalBus()
    q = await bus.subscribe()
    await bus.unsubscribe(q)
    await bus.publish(TurnClaim("d1", "u1", "hi", None, None, 0.0))
    assert q.empty()


# ── registration ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_returns_device_and_announces():
    arb = Arbitrator(bus=LocalBus())
    q = await arb.bus.subscribe()
    dev = await arb.register("d1", area="kitchen", person="darrell")
    assert dev.device_id == "d1"
    assert dev.area == "kitchen"
    assert dev.person == "darrell"
    msg = await asyncio.wait_for(q.get(), timeout=1)
    assert isinstance(msg, DeviceAnnounce)
    assert msg.device_id == "d1"
    assert msg.area == "kitchen"


@pytest.mark.asyncio
async def test_register_update_area_person():
    arb = Arbitrator(bus=LocalBus())
    dev = await arb.register("d1", area="kitchen")
    dev2 = await arb.register("d1", area="living_room", person="darrell")
    assert dev is dev2  # same handle
    assert dev.area == "living_room"
    assert dev.person == "darrell"
    assert arb.devices["d1"].area == "living_room"


@pytest.mark.asyncio
async def test_unregister_releases_won_utterances():
    arb = Arbitrator(bus=LocalBus())
    await arb.register("d1")
    granted, winner = await arb.claim_turn("d1", "u1", "hello")
    assert granted is True
    assert winner == "d1"
    assert arb.elected_winner("u1") == "d1"
    await arb.unregister("d1")
    # unregister releases the utterance the device won.
    assert arb.elected_winner("u1") is None
    assert "d1" not in arb.devices


# ── first_to_endpoint ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_to_endpoint_first_claim_wins():
    arb = Arbitrator(bus=LocalBus(), policy=POLICY_FIRST_TO_ENDPOINT)
    await arb.register("d1")
    await arb.register("d2")
    granted, winner = await arb.claim_turn("d1", "u1", "hello")
    assert granted is True
    assert winner == "d1"
    granted, winner = await arb.claim_turn("d2", "u1", "hello")
    assert granted is False
    assert winner == "d1"


@pytest.mark.asyncio
async def test_first_to_endpoint_concurrent_claims_exactly_one_winner():
    arb = Arbitrator(bus=LocalBus(), policy=POLICY_FIRST_TO_ENDPOINT)
    await arb.register("d1")
    await arb.register("d2")
    await arb.register("d3")
    results = await _gather(arb, [("d1", "u1"), ("d2", "u1"), ("d3", "u1")])
    winners = [r for r in results if r[0]]
    assert len(winners) == 1  # exactly one winner
    denied = [r for r in results if not r[0]]
    assert len(denied) == 2
    assert all(r[1] == winners[0][1] for r in denied)  # all losers name the same winner


@pytest.mark.asyncio
async def test_first_to_endpoint_different_utterances_elect_separate_winners():
    arb = Arbitrator(bus=LocalBus(), policy=POLICY_FIRST_TO_ENDPOINT)
    await arb.register("d1")
    await arb.register("d2")
    g1, w1 = await arb.claim_turn("d1", "u1", "one")
    g2, w2 = await arb.claim_turn("d2", "u2", "two")
    assert g1 is True and w1 == "d1"
    assert g2 is True and w2 == "d2"


# ── proximity ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_proximity_same_area_wins():
    arb = Arbitrator(bus=LocalBus(), policy=POLICY_PROXIMITY)
    await arb.register("d1", area="kitchen", person="darrell")
    await arb.register("d2", area="living_room", person="darrell")
    # d1 is in the kitchen; darrell is in the kitchen -> d1 wins
    granted, winner = await arb.claim_turn("d1", "u1", "hello")
    assert granted is True
    assert winner == "d1"
    granted, winner = await arb.claim_turn("d2", "u1", "hello")
    assert granted is False
    assert winner == "d1"


@pytest.mark.asyncio
async def test_proximity_majority_area_wins_over_first_claim():
    """Concurrent claims: the living-room device claims first, but two kitchen
    devices form the majority area for the speaker, so a kitchen device wins."""
    arb = Arbitrator(bus=LocalBus(), policy=POLICY_PROXIMITY)
    await arb.register("d1", area="kitchen", person="darrell")
    await arb.register("d2", area="kitchen", person="darrell")
    await arb.register("d3", area="living_room", person="darrell")
    results = await _gather(arb, [("d3", "u1"), ("d1", "u1"), ("d2", "u1")])
    winners = [r for r in results if r[0]]
    assert len(winners) == 1
    assert winners[0][1] in ("d1", "d2")  # a kitchen device wins


@pytest.mark.asyncio
async def test_proximity_wins_bookkeeping_tracks_real_winner():
    """``_wins`` must record the elected device, not the tentative first claimant.

    d3 (living_room) claims first and is recorded tentatively, but a kitchen device
    wins on area consensus. Unregistering the real winner must release the utterance;
    unregistering the tentative loser must not.
    """
    arb = Arbitrator(bus=LocalBus(), policy=POLICY_PROXIMITY)
    await arb.register("d1", area="kitchen", person="darrell")
    await arb.register("d2", area="kitchen", person="darrell")
    await arb.register("d3", area="living_room", person="darrell")
    results = await _gather(arb, [("d3", "u1"), ("d1", "u1"), ("d2", "u1")])
    winner = next(w for g, w in results if g)
    assert winner in ("d1", "d2")
    assert "u1" in arb._wins[winner]
    assert "u1" not in arb._wins.get("d3", set())

    # A losing device's unregister must not release the utterance.
    await arb.unregister("d3")
    assert arb.elected_winner("u1") == winner

    # The real winner's unregister releases it.
    await arb.unregister(winner)
    assert arb.elected_winner("u1") is None


@pytest.mark.asyncio
async def test_proximity_falls_back_to_first_claim():
    arb = Arbitrator(bus=LocalBus(), policy=POLICY_PROXIMITY)
    await arb.register("d1", area="kitchen")
    await arb.register("d2", area="kitchen")  # same area, no person info
    results = await _gather(arb, [("d1", "u1"), ("d2", "u1")])
    winners = [r for r in results if r[0]]
    assert len(winners) == 1


# ── release / re-election ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_release_turn_allows_re_election():
    arb = Arbitrator(bus=LocalBus(), policy=POLICY_FIRST_TO_ENDPOINT)
    await arb.register("d1")
    await arb.register("d2")
    granted, winner = await arb.claim_turn("d1", "u1", "hello")
    assert granted is True
    assert arb.elected_winner("u1") == "d1"
    await arb.release_turn("d1", "u1")
    assert arb.elected_winner("u1") is None
    # Now d2 can win the same utterance.
    granted, winner = await arb.claim_turn("d2", "u1", "hello again")
    assert granted is True
    assert winner == "d2"


@pytest.mark.asyncio
async def test_release_turn_by_non_winner_is_noop():
    arb = Arbitrator(bus=LocalBus(), policy=POLICY_FIRST_TO_ENDPOINT)
    await arb.register("d1")
    await arb.register("d2")
    await arb.claim_turn("d1", "u1", "hello")
    await arb.release_turn("d2", "u1")  # d2 did not win u1
    assert arb.elected_winner("u1") == "d1"


# ── introspection ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_is_winner():
    arb = Arbitrator(bus=LocalBus(), policy=POLICY_FIRST_TO_ENDPOINT)
    await arb.register("d1")
    await arb.claim_turn("d1", "u1", "hello")
    assert arb.is_winner("d1", "u1") is True
    assert arb.is_winner("d2", "u1") is False


def test_unknown_policy_raises():
    with pytest.raises(ValueError):
        Arbitrator(bus=LocalBus(), policy="bogus")
