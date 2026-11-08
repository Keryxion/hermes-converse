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
    TurnReleased, POLICY_FIRST_TO_ENDPOINT, POLICY_WAKE_SIGNAL,
)


# ── helpers ──────────────────────────────────────────────────────────────────────────────────

async def _gather(arbitrator, claims):
    """Run several claim_turn calls concurrently and return their (granted, winner) results.

    *claims* is a list of ``(device_id, utterance_id)`` or ``(device_id, utterance_id, kwargs)``.
    """
    calls = []
    for i, c in enumerate(claims):
        device_id, utt, *rest = c
        kwargs = rest[0] if rest else {}
        calls.append(arbitrator.claim_turn(device_id, utt, f"transcript {i}", **kwargs))
    return await asyncio.gather(*calls)


# ── bus ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_local_bus_fan_out():
    bus = LocalBus()
    q1 = await bus.subscribe()
    q2 = await bus.subscribe()
    await bus.publish(TurnClaim("d1", "u1", "hi", 0.0))
    msgs = [await asyncio.wait_for(q1.get(), timeout=1),
            await asyncio.wait_for(q2.get(), timeout=1)]
    assert all(isinstance(m, TurnClaim) for m in msgs)


@pytest.mark.asyncio
async def test_local_bus_unsubscribe_stops_delivery():
    bus = LocalBus()
    q = await bus.subscribe()
    await bus.unsubscribe(q)
    await bus.publish(TurnClaim("d1", "u1", "hi", 0.0))
    assert q.empty()


# ── registration ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_returns_device_and_announces():
    arb = Arbitrator(bus=LocalBus())
    q = await arb.bus.subscribe()
    dev = await arb.register("d1")
    assert dev.device_id == "d1"
    msg = await asyncio.wait_for(q.get(), timeout=1)
    assert isinstance(msg, DeviceAnnounce)
    assert msg.device_id == "d1"


@pytest.mark.asyncio
async def test_register_twice_returns_same_handle():
    arb = Arbitrator(bus=LocalBus())
    dev = await arb.register("d1")
    dev2 = await arb.register("d1")
    assert dev is dev2
    assert arb.devices["d1"] is dev


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


@pytest.mark.asyncio
async def test_first_to_endpoint_ignores_wake_word():
    """Wake-word grouping is a wake_signal-only behaviour: under first_to_endpoint, claims with
    different wake words for the same utterance still compete (first claim wins outright)."""
    arb = Arbitrator(bus=LocalBus(), policy=POLICY_FIRST_TO_ENDPOINT)
    await arb.register("d1")
    await arb.register("d2")
    granted, winner = await arb.claim_turn("d1", "u1", "hello", wake_word="arya")
    assert granted is True
    granted, winner = await arb.claim_turn("d2", "u1", "hello", wake_word="echo")
    assert granted is False
    assert winner == "d1"


# ── wake_signal ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wake_signal_higher_confidence_wins():
    arb = Arbitrator(bus=LocalBus(), policy=POLICY_WAKE_SIGNAL)
    await arb.register("d1")
    await arb.register("d2")
    results = await _gather(arb, [
        ("d1", "u1", {"wake_word": "arya", "confidence": 0.60, "heard_at": 100.0}),
        ("d2", "u1", {"wake_word": "arya", "confidence": 0.95, "heard_at": 101.0}),
    ])
    winners = [r for r in results if r[0]]
    assert len(winners) == 1
    assert winners[0][1] == "d2"  # higher confidence, even though it heard it slightly later


@pytest.mark.asyncio
async def test_wake_signal_different_wake_words_both_win():
    """Different wake words are not in competition: each elects independently."""
    arb = Arbitrator(bus=LocalBus(), policy=POLICY_WAKE_SIGNAL)
    await arb.register("d1")
    await arb.register("d2")
    g1, w1 = await arb.claim_turn("d1", "u1", "turn off the lights", wake_word="arya", confidence=0.5)
    g2, w2 = await arb.claim_turn("d2", "u1", "turn off the lights", wake_word="echo", confidence=0.5)
    assert g1 is True and w1 == "d1"
    assert g2 is True and w2 == "d2"
    assert arb.elected_winner("u1", wake_word="arya") == "d1"
    assert arb.elected_winner("u1", wake_word="echo") == "d2"


@pytest.mark.asyncio
async def test_wake_signal_volume_breaks_confidence_tie():
    arb = Arbitrator(bus=LocalBus(), policy=POLICY_WAKE_SIGNAL)
    await arb.register("d1")
    await arb.register("d2")
    results = await _gather(arb, [
        ("d1", "u1", {"wake_word": "arya", "confidence": 0.9, "volume": 1200.0}),
        ("d2", "u1", {"wake_word": "arya", "confidence": 0.9, "volume": 4500.0}),
    ])
    winners = [r for r in results if r[0]]
    assert len(winners) == 1
    assert winners[0][1] == "d2"  # same confidence, louder capture wins


@pytest.mark.asyncio
async def test_wake_signal_heard_at_breaks_remaining_tie():
    arb = Arbitrator(bus=LocalBus(), policy=POLICY_WAKE_SIGNAL)
    await arb.register("d1")
    await arb.register("d2")
    results = await _gather(arb, [
        ("d1", "u1", {"wake_word": "arya", "confidence": 0.9, "volume": 3000.0, "heard_at": 500.0}),
        ("d2", "u1", {"wake_word": "arya", "confidence": 0.9, "volume": 3000.0, "heard_at": 200.0}),
    ])
    winners = [r for r in results if r[0]]
    assert len(winners) == 1
    assert winners[0][1] == "d2"  # same confidence and volume, heard it first


@pytest.mark.asyncio
async def test_wake_signal_missing_fields_fall_back_to_claim_order():
    """A device that reports none of the wake_signal fields still arbitrates — it just loses any
    tier it has no data for, falling back to whoever claimed first on the server."""
    arb = Arbitrator(bus=LocalBus(), policy=POLICY_WAKE_SIGNAL)
    await arb.register("d1")
    await arb.register("d2")
    results = await _gather(arb, [("d1", "u1"), ("d2", "u1")])
    winners = [r for r in results if r[0]]
    assert len(winners) == 1
    assert winners[0][1] == "d1"  # claimed first, nobody reported any signal


@pytest.mark.asyncio
async def test_wake_signal_wins_bookkeeping_tracks_real_winner():
    """``_wins`` must record the elected device, not the tentative first claimant.

    d1 claims first (low confidence) and is recorded tentatively, but d2 wins on confidence.
    Unregistering the real winner must release the utterance; unregistering the tentative loser
    must not.
    """
    arb = Arbitrator(bus=LocalBus(), policy=POLICY_WAKE_SIGNAL)
    await arb.register("d1")
    await arb.register("d2")
    results = await _gather(arb, [
        ("d1", "u1", {"wake_word": "arya", "confidence": 0.3}),
        ("d2", "u1", {"wake_word": "arya", "confidence": 0.9}),
    ])
    winner = next(w for g, w in results if g)
    assert winner == "d2"
    assert ("u1", "arya") in arb._wins[winner]
    assert ("u1", "arya") not in arb._wins.get("d1", set())

    # The tentative (losing) claimant's unregister must not release the utterance.
    await arb.unregister("d1")
    assert arb.elected_winner("u1", wake_word="arya") == winner

    # The real winner's unregister releases it.
    await arb.unregister(winner)
    assert arb.elected_winner("u1", wake_word="arya") is None


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


@pytest.mark.asyncio
async def test_release_turn_releases_the_right_wake_word_group():
    """Under wake_signal, two devices can each own an independent election for the SAME
    utterance_id (one per wake word) at once; releasing one must not touch the other."""
    arb = Arbitrator(bus=LocalBus(), policy=POLICY_WAKE_SIGNAL)
    await arb.register("d1")
    await arb.register("d2")
    await arb.claim_turn("d1", "u1", "hello", wake_word="arya")
    await arb.claim_turn("d2", "u1", "hello", wake_word="echo")
    await arb.release_turn("d1", "u1")
    assert arb.elected_winner("u1", wake_word="arya") is None
    assert arb.elected_winner("u1", wake_word="echo") == "d2"


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


def test_proximity_policy_no_longer_accepted():
    """``proximity`` was removed in favour of ``wake_signal``; constructing with it directly is an
    error (settings.py's ``_arbitrate_policy`` is what falls a config value back to the default)."""
    with pytest.raises(ValueError):
        Arbitrator(bus=LocalBus(), policy="proximity")
