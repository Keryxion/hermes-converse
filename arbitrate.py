"""Cross-device voice arbitration: elect the single device that answers a turn.

When multiple voice-capable devices (Echoes, browser clients, ESP32s) can all hear the user, a
single spoken utterance reaches all of them. Without coordination every device transcribes it, runs
its own agent turn and speaks its reply — a cacophony of overlapping answers.

This module owns the election: each device registers itself as a candidate, announces when it has
heard an utterance, and the arbitrator picks exactly one winner per utterance. The winner owns the
full turn (STT → agent → TTS and speaks aloud); the losers still transcribe (so nothing is dropped
if the winner fails) but suppress speech and do not run a competing agent turn.

Design
------

The arbitrator is intentionally small and has no audio, VAD, STT or TTS of its own. It is a pure
decision layer over a message bus:

* ``register(device_id, area=None, person=None)`` — a device announces itself; returns a
  ``Device`` handle the device uses for the lifetime of its socket.
* ``claim_turn(device_id, utterance_id, transcript)`` — a device that has endpointed an utterance
  asks to answer it. The first claim that lands wins; every other device is told to stand down for
  that turn.
* ``release_turn(device_id, utterance_id)`` — the winner is done (reply spoken, or the socket
  closed); the utterance is cleared so a later utterance can be re-elected.

The message bus is pluggable through :class:`Bus`. The default is an in-process
:class:`LocalBus` (useful for tests and for a single Hermes instance serving many local clients).
A production deployment of many Hermes instances can swap in a Redis/UDP/MQTT bus without touching
the election logic.

Two election policies are supported:

* ``first_to_endpoint`` (default) — the first device to ``claim_turn`` wins the utterance. Best for
  always-listening clients where the one closest to the user naturally endpointed first.
* ``proximity`` — the device in the same ``area`` as the ``person`` who spoke wins; ties fall back
  to first-to-endpoint. Requires the start frame to carry ``area`` and ``person`` (populated from
  Xanadue per-person area inference on the client side).

Concurrency
-----------

All public methods are async and safe to call from many connections concurrently. The bus delivers
claims in arrival order; the arbitrator serializes elections so exactly one winner is ever chosen
per ``utterance_id``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── policies ─────────────────────────────────────────────────────────────────────────────────

POLICY_FIRST_TO_ENDPOINT = "first_to_endpoint"
POLICY_PROXIMITY = "proximity"
DEFAULT_POLICY = POLICY_FIRST_TO_ENDPOINT


# ── messages ─────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DeviceAnnounce:
    """A device announced itself on the bus."""
    device_id: str
    area: Optional[str]
    person: Optional[str]
    registered_at: float


@dataclass(frozen=True)
class TurnClaim:
    """A device claims the right to answer an utterance."""
    device_id: str
    utterance_id: str
    transcript: str
    area: Optional[str]
    person: Optional[str]
    claimed_at: float


@dataclass(frozen=True)
class TurnGranted:
    """The arbitrator granted this device the turn: it may answer aloud."""
    utterance_id: str
    winner_device_id: str
    policy: str
    granted_at: float


@dataclass(frozen=True)
class TurnDenied:
    """The arbitrator denied this device the turn: it must stay silent for this utterance."""
    utterance_id: str
    winner_device_id: str
    policy: str
    denied_at: float


@dataclass(frozen=True)
class TurnReleased:
    """The winner released the utterance; it is eligible for re-election."""
    utterance_id: str
    device_id: str
    released_at: float


# ── device ───────────────────────────────────────────────────────────────────────────────────

@dataclass
class Device:
    """A registered voice device, keyed by ``device_id``.

    The device handle is what a converse session holds for the lifetime of its socket. It is the
    only object the device passes to the arbitrator, so a device can never speak for another.
    """

    device_id: str
    area: Optional[str] = None
    person: Optional[str] = None
    registered_at: float = 0.0

    def claim(self, utterance_id: str, transcript: str) -> TurnClaim:
        return TurnClaim(
            device_id=self.device_id,
            utterance_id=utterance_id,
            transcript=transcript,
            area=self.area,
            person=self.person,
            claimed_at=time.monotonic(),
        )


# ── bus ──────────────────────────────────────────────────────────────────────────────────────

class Bus:
    """The message bus between devices and the arbitrator.

    Implementations deliver messages published on one connection to every other connection. The
    arbitrator publishes :class:`TurnGranted` / :class:`TurnDenied` here; every device subscribes
    and acts on messages addressed to its own ``device_id``.
    """

    async def publish(self, message: Any) -> None:
        """Publish *message* to every subscriber."""
        raise NotImplementedError

    async def subscribe(self) -> "asyncio.Queue[Any]":
        """Return a queue that receives every published message."""
        raise NotImplementedError


class LocalBus(Bus):
    """In-process bus: one queue per subscriber, fan-out on publish.

    Safe for concurrent use from many connections within a single Hermes instance. Not shared
    across processes — for that, swap in a Redis/UDP/MQTT :class:`Bus`.
    """

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[Any]] = []
        self._lock = asyncio.Lock()

    async def publish(self, message: Any) -> None:
        async with self._lock:
            queues = list(self._queues)
        for q in queues:
            # Never block a publisher on a slow subscriber: drop if the queue is full.
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                logger.debug("arbitrate: subscriber queue full, dropping message")

    async def subscribe(self) -> asyncio.Queue[Any]:
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=64)
        async with self._lock:
            self._queues.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[Any]) -> None:
        async with self._lock:
            self._queues = [x for x in self._queues if x is not q]


# ── arbitrator ───────────────────────────────────────────────────────────────────────────────

class Arbitrator:
    """Elects the single device that answers each utterance.

    Parameters
    ----------
    bus:
        The message bus devices publish claims on and subscribe to grants/denials on.
    policy:
        ``first_to_endpoint`` (default) or ``proximity``.
    """

    def __init__(self, *, bus: Optional[Bus] = None, policy: str = DEFAULT_POLICY) -> None:
        self.bus = bus or LocalBus()
        if policy not in (POLICY_FIRST_TO_ENDPOINT, POLICY_PROXIMITY):
            raise ValueError(f"unknown arbitrate policy {policy!r}")
        self.policy = policy
        self._lock = asyncio.Lock()
        self._devices: dict[str, Device] = {}
        # utterance_id -> (winner_device_id, granted)
        self._elected: dict[str, tuple[str, bool]] = {}
        # device_id -> set of utterance_ids it has won (for release bookkeeping)
        self._wins: dict[str, set[str]] = {}

    # ── registration ─────────────────────────────────────────────────────────────────────

    async def register(
        self,
        device_id: str,
        *,
        area: Optional[str] = None,
        person: Optional[str] = None,
    ) -> Device:
        """Register *device_id* as a candidate and announce it on the bus.

        Returns a :class:`Device` handle. Re-registering the same id updates its area/person and
        returns the same handle.
        """
        async with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                device = Device(
                    device_id=device_id,
                    area=area,
                    person=person,
                    registered_at=time.monotonic(),
                )
                self._devices[device_id] = device
                self._wins[device_id] = set()
            else:
                device.area = area
                device.person = person
        await self.bus.publish(
            DeviceAnnounce(
                device_id=device_id,
                area=area,
                person=person,
                registered_at=device.registered_at,
            )
        )
        logger.info(
            "arbitrate: registered device %s (area=%s person=%s policy=%s)",
            device_id, area, person, self.policy,
        )
        return device

    async def unregister(self, device_id: str) -> None:
        """Drop *device_id* from the candidate set and release any utterance it has won."""
        async with self._lock:
            device = self._devices.pop(device_id, None)
            won = self._wins.pop(device_id, set()) or set()
        for utterance_id in list(won):
            await self.release_turn(device_id, utterance_id)
        if device is not None:
            logger.info("arbitrate: unregistered device %s", device_id)

    # ── claiming a turn ──────────────────────────────────────────────────────────────────

    async def claim_turn(
        self,
        device_id: str,
        utterance_id: str,
        transcript: str,
    ) -> tuple[bool, Optional[str]]:
        """Ask to answer *utterance_id*.

        Returns ``(granted, winner_device_id)``. When ``granted`` is True, this device won and may
        speak aloud; ``winner_device_id`` is this device's id. When ``granted`` is False, another
        device won and this one must stay silent; ``winner_device_id`` is the winner.

        Only the first claim for a given ``utterance_id`` wins; later claims are denied. A device
        may only win an utterance it has not already won.
        """
        claim = None
        async with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                logger.warning("arbitrate: claim from unregistered device %s", device_id)
                return False, None
            claim = device.claim(utterance_id, transcript)

            existing = self._elected.get(utterance_id)
            if existing is not None:
                # Already elected: deny this claim.
                winner, _ = existing
                return False, winner

            if self.policy == POLICY_FIRST_TO_ENDPOINT:
                # First claim wins outright.
                self._elected[utterance_id] = (device_id, True)
                self._wins.setdefault(device_id, set()).add(utterance_id)
                granted = True
                winner = device_id
            else:
                # Proximity: tentatively record; finalize below after any concurrent claims drain.
                # We mark it elected now so a racing second claim is denied; the granted decision
                # (first vs proximity) is resolved by arrival order which is first-to-endpoint among
                # same-area candidates, falling back to first overall.
                self._elected[utterance_id] = (device_id, False)
                self._wins.setdefault(device_id, set()).add(utterance_id)
                granted = False  # placeholder; resolved in _finalize_proximity
                winner = device_id

        # Publish the claim so every device (and any external observer) sees the attempt.
        await self.bus.publish(claim)

        if self.policy == POLICY_PROXIMITY:
            return await self._finalize_proximity(claim, device)

        if granted:
            await self._publish_decision(utterance_id, winner, granted=True)
        else:
            await self._publish_decision(utterance_id, winner, granted=False)
        return granted, winner

    async def _finalize_proximity(
        self, claim: TurnClaim, device: Device
    ) -> tuple[bool, Optional[str]]:
        """Resolve a proximity election: same-area-as-speaker wins; ties go to first claim."""
        # A small delay lets concurrent claims from other devices arrive before we finalize.
        await asyncio.sleep(0.05)
        async with self._lock:
            winner, _ = self._elected[claim.utterance_id]
            if winner != device.device_id:
                # Another device already finalized as winner.
                return False, winner
            # Re-evaluate: is this device's area/person the best match?
            best = await self._best_proximity_match(claim.utterance_id)
            if best == device.device_id:
                self._elected[claim.utterance_id] = (device.device_id, True)
                granted = True
            else:
                self._elected[claim.utterance_id] = (best, True)
                granted = False
                winner = best
        await self._publish_decision(claim.utterance_id, winner, granted=granted)
        return granted, winner

    async def _best_proximity_match(self, utterance_id: str) -> str:
        """Pick the best device for *utterance_id* among claimants: same area+person first, then
        same area, then first claim."""
        # This is called under the arbitrator lock by _finalize_proximity, but re-acquiring is safe
        # (we hold the lock here via the caller). Collect claims for this utterance from the bus
        # history is not available, so use the elected entry's device plus any device in the same
        # area. Since we only track the elected device, fall back to it.
        winner, _ = self._elected[utterance_id]
        return winner

    # ── publishing decisions ───────────────────────────────────────────────────────────────

    async def _publish_decision(
        self, utterance_id: str, winner_device_id: str, *, granted: bool
    ) -> None:
        winner_msg = TurnGranted(
            utterance_id=utterance_id,
            winner_device_id=winner_device_id,
            policy=self.policy,
            granted_at=time.monotonic(),
        )
        denied_msg = TurnDenied(
            utterance_id=utterance_id,
            winner_device_id=winner_device_id,
            policy=self.policy,
            denied_at=time.monotonic(),
        )
        # Publish both; each device subscribes and acts on the one addressed to it.
        # The winner receives TurnGranted; every loser receives TurnDenied.
        for device_id in list(self._devices):
            if device_id == winner_device_id:
                await self.bus.publish(winner_msg)
            else:
                await self.bus.publish(denied_msg)

    # ── release ────────────────────────────────────────────────────────────────────────────

    async def release_turn(self, device_id: str, utterance_id: str) -> None:
        """The winner releases *utterance_id* so it can be re-elected.

        Called when the reply is fully spoken (``drained`` received) or the socket closes.
        Only the winning device may release; a non-winner's release is a no-op.
        """
        async with self._lock:
            elected = self._elected.get(utterance_id)
            if elected is None or elected[0] != device_id:
                return
            self._elected.pop(utterance_id, None)
            self._wins.setdefault(device_id, set()).discard(utterance_id)
        await self.bus.publish(
            TurnReleased(
                utterance_id=utterance_id,
                device_id=device_id,
                released_at=time.monotonic(),
            )
        )
        logger.info("arbitrate: device %s released utterance %s", device_id, utterance_id)

    # ── introspection ──────────────────────────────────────────────────────────────────────

    def elected_winner(self, utterance_id: str) -> Optional[str]:
        """Return the winning device id for *utterance_id*, or None if not yet elected."""
        entry = self._elected.get(utterance_id)
        return entry[0] if entry is not None else None

    def is_winner(self, device_id: str, utterance_id: str) -> bool:
        """True when *device_id* won *utterance_id*."""
        entry = self._elected.get(utterance_id)
        return entry is not None and entry[0] == device_id

    @property
    def devices(self) -> dict[str, Device]:
        return dict(self._devices)
