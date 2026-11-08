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

* ``register(device_id)`` — a device announces itself; returns a ``Device`` handle the device uses
  for the lifetime of its socket.
* ``claim_turn(device_id, utterance_id, transcript, *, wake_word=None, confidence=None,
  heard_at=None, volume=None)`` — a device that has endpointed an utterance asks to answer it.
  Every other device claiming the same utterance (and, under ``wake_signal``, the same wake word)
  is told to stand down for that turn.
* ``release_turn(device_id, utterance_id)`` — the winner is done (reply spoken, or the socket
  closed); the utterance is cleared so a later utterance can be re-elected.

The message bus is pluggable through :class:`Bus`. The default is an in-process
:class:`LocalBus` (useful for tests and for a single Hermes instance serving many local clients).
A production deployment of many Hermes instances can swap in a Redis/UDP/MQTT bus without touching
the election logic.

Two election policies are supported:

* ``first_to_endpoint`` (default) — the first device to ``claim_turn`` wins the utterance. Best for
  always-listening clients where the one closest to the user naturally endpointed first.
* ``wake_signal`` — claims for the same utterance are first split by ``wake_word``: devices that
  heard a *different* wake word are not competing (two different assistants were addressed, or the
  transcripts merely collided) and each such group is elected independently. Within a group, the
  claimant with the highest wake-word detection ``confidence`` wins; ties fall back to the loudest
  captured ``volume``, then to the earliest ``heard_at`` (the client's wall-clock wake timestamp),
  then to whoever's claim reached the server first. Any field a device omits sorts as "worst" on
  that tier rather than crashing, so older clients that send none of this still arbitrate (by
  ``heard_at``/first-claim order, same as a bare ``first_to_endpoint`` tie).

  There used to be a third policy, ``proximity``, that guessed the speaker's location by a majority
  vote over each client's self-reported ``area`` string. It never had a real signal to work from —
  no client actually measured proximity — so it has been replaced by ``wake_signal`` above.

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
POLICY_WAKE_SIGNAL = "wake_signal"
DEFAULT_POLICY = POLICY_FIRST_TO_ENDPOINT

# How long the server collects concurrent wake claims for the same wake word before electing the
# primary device. Long enough to cover detector jitter across devices, short enough that the client
# is not left waiting before it starts streaming.
DEFAULT_WAKE_WINDOW = 0.25


# ── messages ─────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DeviceAnnounce:
    """A device announced itself on the bus."""
    device_id: str
    registered_at: float


@dataclass(frozen=True)
class TurnClaim:
    """A device claims the right to answer an utterance.

    ``wake_word``/``confidence``/``heard_at``/``volume`` are the ``wake_signal`` policy's inputs;
    all four are optional and ``None`` when a device doesn't report them (older clients, or a
    session-mode turn that followed on from a conversation without a fresh wake-word hit).
    """
    device_id: str
    utterance_id: str
    transcript: str
    claimed_at: float
    wake_word: Optional[str] = None
    confidence: Optional[float] = None
    heard_at: Optional[float] = None
    volume: Optional[float] = None


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


@dataclass(frozen=True)
class WakeClaim:
    """A device asks, at wake-word time, whether it is the primary box for this wake word.

    ``wake_word``/``confidence``/``volume``/``heard_at`` are the cooperative signals the client
    reports the moment its local detector fires. ``volume`` may also be supplied server-side when
    the client has no level meter. ``claim_id`` is echoed back to the client so it can correlate the
    grant/deny response with the wake hit that produced it.
    """
    claim_id: str
    device_id: str
    wake_word: Optional[str]
    confidence: Optional[float]
    heard_at: Optional[float]
    volume: Optional[float]
    claimed_at: float


# ── device ───────────────────────────────────────────────────────────────────────────────────

@dataclass
class Device:
    """A registered voice device, keyed by ``device_id``.

    The device handle is what a converse session holds for the lifetime of its socket. It is the
    only object the device passes to the arbitrator, so a device can never speak for another.
    """

    device_id: str
    registered_at: float = 0.0

    def claim(
        self,
        utterance_id: str,
        transcript: str,
        *,
        wake_word: Optional[str] = None,
        confidence: Optional[float] = None,
        heard_at: Optional[float] = None,
        volume: Optional[float] = None,
    ) -> TurnClaim:
        return TurnClaim(
            device_id=self.device_id,
            utterance_id=utterance_id,
            transcript=transcript,
            claimed_at=time.monotonic(),
            wake_word=wake_word,
            confidence=confidence,
            heard_at=heard_at,
            volume=volume,
        )

    def wake_claim(
        self,
        *,
        wake_word: Optional[str] = None,
        confidence: Optional[float] = None,
        heard_at: Optional[float] = None,
        volume: Optional[float] = None,
    ) -> WakeClaim:
        return WakeClaim(
            claim_id=uuid.uuid4().hex,
            device_id=self.device_id,
            wake_word=wake_word,
            confidence=confidence,
            heard_at=heard_at,
            volume=volume,
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

class _WakeElection:
    """One wake-time election: claims for the same wake word collected inside a short window."""

    __slots__ = ("wake_word", "created_at", "claims", "done", "winner")

    def __init__(self, wake_word: Optional[str], created_at: float) -> None:
        self.wake_word = wake_word
        self.created_at = created_at
        self.claims: list[WakeClaim] = []
        self.done = asyncio.Event()
        self.winner: Optional[str] = None


class Arbitrator:
    """Elects the single device that answers each utterance.

    Parameters
    ----------
    bus:
        The message bus devices publish claims on and subscribe to grants/denials on.
    policy:
        ``first_to_endpoint`` (default) or ``wake_signal``.
    wake_window:
        Seconds to collect concurrent wake claims for the same wake word before electing the
        primary device. ``0`` elects as soon as the first claim lands (still giving claims in the
        same event-loop tick a chance to join).
    """

    def __init__(self, *, bus: Optional[Bus] = None, policy: str = DEFAULT_POLICY,
                 wake_window: float = DEFAULT_WAKE_WINDOW) -> None:
        self.bus = bus or LocalBus()
        if policy not in (POLICY_FIRST_TO_ENDPOINT, POLICY_WAKE_SIGNAL):
            raise ValueError(f"unknown arbitrate policy {policy!r}")
        self.policy = policy
        self.wake_window = max(0.0, float(wake_window))
        self._lock = asyncio.Lock()
        self._devices: dict[str, Device] = {}
        # election_key -> (winner_device_id, granted). Under first_to_endpoint the key is always
        # (utterance_id, None); under wake_signal it is (utterance_id, wake_word), so claims for the
        # same utterance but a different wake word elect independently instead of competing.
        self._elected: dict[tuple[str, Optional[str]], tuple[str, bool]] = {}
        # device_id -> set of election_keys it has won (for release bookkeeping)
        self._wins: dict[str, set[tuple[str, Optional[str]]]] = {}
        # election_key -> list of TurnClaim (every claimant, for wake_signal ranking)
        self._claims: dict[tuple[str, Optional[str]], list[TurnClaim]] = {}
        # election_key -> asyncio.Event set when wake_signal finalization completes
        self._finalized: dict[tuple[str, Optional[str]], asyncio.Event] = {}
        # election_key -> winning device_id (wake_signal outcome)
        self._wake_winner: dict[tuple[str, Optional[str]], str] = {}
        # wake_word -> the wake-time election currently collecting claims. A claim that arrives after
        # the window closed starts a fresh election (a new spoken wake hit), never joins the old one.
        self._wake_open: dict[Optional[str], "_WakeElection"] = {}

    def _election_key(self, utterance_id: str, wake_word: Optional[str]) -> tuple[str, Optional[str]]:
        """The key claims are grouped and elected under: just the utterance under
        ``first_to_endpoint`` (wake word is irrelevant to that policy), or utterance+wake-word under
        ``wake_signal`` so different wake words never suppress each other."""
        if self.policy != POLICY_WAKE_SIGNAL:
            return (utterance_id, None)
        return (utterance_id, wake_word)

    # ── registration ─────────────────────────────────────────────────────────────────────

    async def register(self, device_id: str) -> Device:
        """Register *device_id* as a candidate and announce it on the bus.

        Returns a :class:`Device` handle. Re-registering the same id returns the same handle.
        """
        async with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                device = Device(device_id=device_id, registered_at=time.monotonic())
                self._devices[device_id] = device
                self._wins[device_id] = set()
        await self.bus.publish(DeviceAnnounce(device_id=device_id, registered_at=device.registered_at))
        logger.info("arbitrate: registered device %s (policy=%s)", device_id, self.policy)
        return device

    async def unregister(self, device_id: str) -> None:
        """Drop *device_id* from the candidate set and release any utterance it has won."""
        async with self._lock:
            device = self._devices.pop(device_id, None)
            # A snapshot, not a pop: release_turn looks the election key up through this same
            # ``_wins[device_id]`` set and discards from it as it releases, so the entry must
            # still be there for each call below to find.
            won = set(self._wins.get(device_id, set()))
        for utterance_id, _wake_word in list(won):
            await self.release_turn(device_id, utterance_id)
        async with self._lock:
            self._wins.pop(device_id, None)
        if device is not None:
            logger.info("arbitrate: unregistered device %s", device_id)

    # ── claiming at wake time (cooperative handshake) ───────────────────────────────────

    async def claim_wake(
        self,
        device_id: str,
        *,
        wake_word: Optional[str] = None,
        confidence: Optional[float] = None,
        heard_at: Optional[float] = None,
        volume: Optional[float] = None,
    ) -> tuple[bool, Optional[str], str]:
        """Ask, the moment this device's local wake detector fires, whether it is the primary box.

        Every device that heard the same wake word inside ``wake_window`` competes in one election:
        highest ``confidence`` wins, then loudest ``volume``, then earliest ``heard_at`` (the
        client's wall clock at the wake hit), then whichever claim reached the server first.
        Different wake words elect independently, so two assistants addressed at once both proceed.

        Returns ``(granted, winner_device_id, claim_id)``. ``claim_id`` identifies this specific
        claim so the client can correlate the response with the wake hit that produced it. A claim
        that arrives after the window closed starts a *new* election — it is a new spoken wake hit,
        not a late member of the old one.
        """
        async with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                logger.warning("arbitrate: wake claim from unregistered device %s", device_id)
                return False, None, ""
            now = time.monotonic()
            election = self._wake_open.get(wake_word)
            if election is None or now - election.created_at > self.wake_window:
                election = _WakeElection(wake_word, now)
                self._wake_open[wake_word] = election
                # Created under the lock; every claim appended below is in the list before the
                # finalizer can acquire the lock and elect.
                asyncio.ensure_future(self._finalize_wake(election))
            claim = device.wake_claim(
                wake_word=wake_word, confidence=confidence, heard_at=heard_at, volume=volume,
            )
            election.claims.append(claim)
        await self.bus.publish(claim)
        await election.done.wait()
        return election.winner == device_id, election.winner, claim.claim_id

    async def _finalize_wake(self, election: _WakeElection) -> None:
        """Close *election* after its collection window and elect the best-ranked claimant."""
        if self.wake_window > 0:
            await asyncio.sleep(self.wake_window)
        async with self._lock:
            # Only retire the entry if it is still the open election; a late claim may already have
            # replaced it with a fresh one, which must stay open for its own window.
            if self._wake_open.get(election.wake_word) is election:
                self._wake_open.pop(election.wake_word, None)
            if not election.claims:  # defensive: an election is only created with a claim pending
                election.winner = None
            else:
                election.winner = min(election.claims, key=self._rank_wake_signal).device_id
        election.done.set()
        logger.info(
            "arbitrate: wake election for %r -> %s (%d claimant(s))",
            election.wake_word, election.winner, len(election.claims))

    # ── claiming a turn ──────────────────────────────────────────────────────────────────

    async def claim_turn(
        self,
        device_id: str,
        utterance_id: str,
        transcript: str,
        *,
        wake_word: Optional[str] = None,
        confidence: Optional[float] = None,
        heard_at: Optional[float] = None,
        volume: Optional[float] = None,
    ) -> tuple[bool, Optional[str]]:
        """Ask to answer *utterance_id*.

        ``wake_word``/``confidence``/``heard_at``/``volume`` are only consulted under the
        ``wake_signal`` policy (see the module docstring); ``first_to_endpoint`` ignores them.

        Returns ``(granted, winner_device_id)``. When ``granted`` is True, this device won and may
        speak aloud; ``winner_device_id`` is this device's id. When ``granted`` is False, another
        device won and this one must stay silent; ``winner_device_id`` is the winner.
        """
        claim = None
        should_finalize = False
        key = self._election_key(utterance_id, wake_word)
        async with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                logger.warning("arbitrate: claim from unregistered device %s", device_id)
                return False, None
            claim = device.claim(
                utterance_id, transcript,
                wake_word=wake_word, confidence=confidence, heard_at=heard_at, volume=volume,
            )
            self._claims.setdefault(key, []).append(claim)

            existing = self._elected.get(key)
            if existing is not None:
                winner, granted = existing
                if granted:
                    # Already finalized: deny this claim.
                    return False, winner
                # wake_signal finalization in progress: wait for the first claimant's decision.
                should_finalize = False
            else:
                # First claim for this election.
                if self.policy == POLICY_FIRST_TO_ENDPOINT:
                    # First claim wins outright.
                    self._elected[key] = (device_id, True)
                    self._wins.setdefault(device_id, set()).add(key)
                    granted = True
                    winner = device_id
                else:
                    # wake_signal: tentatively record; finalize below once concurrent claims drain.
                    # The real winner is added to ``_wins`` in ``_finalize_wake_signal`` — the
                    # tentative claimant may not be the one that actually wins.
                    self._elected[key] = (device_id, False)
                    self._finalized[key] = asyncio.Event()
                    self._wake_winner[key] = device_id  # placeholder
                    granted = False  # placeholder; resolved in _finalize_wake_signal
                    winner = device_id
                    should_finalize = True

        # Publish the claim so every device (and any external observer) sees the attempt.
        await self.bus.publish(claim)

        if self.policy == POLICY_WAKE_SIGNAL:
            if should_finalize:
                return await self._finalize_wake_signal(claim, device, key)
            # Another claimant is finalizing: wait for the outcome.
            await self._finalized[key].wait()
            winner = self._wake_winner[key]
            granted = (device_id == winner)
            return granted, winner

        if granted:
            await self._publish_decision(utterance_id, winner, granted=True)
        else:
            await self._publish_decision(utterance_id, winner, granted=False)
        return granted, winner

    async def _finalize_wake_signal(
        self, claim: TurnClaim, device: Device, key: tuple[str, Optional[str]]
    ) -> tuple[bool, Optional[str]]:
        """Resolve a wake_signal election: best-ranked claimant (see ``_rank_wake_signal``) wins."""
        # A small delay lets concurrent claims from other devices arrive before we finalize.
        await asyncio.sleep(0.05)
        async with self._lock:
            winner, _ = self._elected[key]
            if winner != device.device_id:
                # Another device already finalized as winner.
                return False, winner
            best = self._best_wake_signal_match(key)
            self._elected[key] = (best, True)
            self._wake_winner[key] = best
            # ``_wins`` tracks utterances a device actually won, so record the real winner
            # (not the tentative claimant recorded at claim time).
            self._wins.setdefault(best, set()).add(key)
            granted = (best == device.device_id)
            winner = best
        self._finalized[key].set()
        await self._publish_decision(claim.utterance_id, winner, granted=granted)
        return granted, winner

    @staticmethod
    def _rank_wake_signal(claim: TurnClaim) -> tuple[float, float, float, float]:
        """Sort key for wake_signal ranking: highest ``confidence`` first, then loudest ``volume``,
        then earliest ``heard_at`` (the client's wake-moment wall clock), then earliest server
        receive order (``claimed_at``, always present, the deterministic final tie-break). A field a
        claimant did not report sorts worst on its own tier rather than winning by default."""
        confidence = claim.confidence if claim.confidence is not None else -1.0
        volume = claim.volume if claim.volume is not None else -1.0
        heard_at = claim.heard_at if claim.heard_at is not None else float("inf")
        return (-confidence, -volume, heard_at, claim.claimed_at)

    def _best_wake_signal_match(self, key: tuple[str, Optional[str]]) -> str:
        """Pick the best device among the claimants sharing *key* (same utterance + wake word)."""
        claims = self._claims.get(key, [])
        if not claims:
            winner, _ = self._elected[key]
            return winner
        return min(claims, key=self._rank_wake_signal).device_id

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

        Called when the reply is fully spoken (``drained`` received) or the socket closes. Only the
        winning device may release; a non-winner's release is a no-op. A device claims at most one
        election per ``utterance_id`` (its own claim has exactly one wake word), so looking the
        election key up by ``utterance_id`` among this device's wins is unambiguous even under
        ``wake_signal``, where several devices may hold independent elections for the same
        ``utterance_id`` (one per distinct wake word) at once.
        """
        async with self._lock:
            won = self._wins.get(device_id, set())
            key = next((k for k in won if k[0] == utterance_id), None)
            if key is None:
                return
            elected = self._elected.get(key)
            if elected is None or elected[0] != device_id:
                return
            self._elected.pop(key, None)
            won.discard(key)
            self._claims.pop(key, None)
            self._finalized.pop(key, None)
            self._wake_winner.pop(key, None)
        await self.bus.publish(
            TurnReleased(
                utterance_id=utterance_id,
                device_id=device_id,
                released_at=time.monotonic(),
            )
        )
        logger.info("arbitrate: device %s released utterance %s", device_id, utterance_id)

    # ── introspection ──────────────────────────────────────────────────────────────────────

    def elected_winner(self, utterance_id: str, wake_word: Optional[str] = None) -> Optional[str]:
        """Return the winning device id for *utterance_id* (optionally scoped to *wake_word* under
        ``wake_signal``), or None if not yet elected."""
        entry = self._elected.get(self._election_key(utterance_id, wake_word))
        return entry[0] if entry is not None else None

    def is_winner(self, device_id: str, utterance_id: str, wake_word: Optional[str] = None) -> bool:
        """True when *device_id* won *utterance_id* (optionally scoped to *wake_word*)."""
        entry = self._elected.get(self._election_key(utterance_id, wake_word))
        return entry is not None and entry[0] == device_id

    @property
    def devices(self) -> dict[str, Device]:
        return dict(self._devices)
