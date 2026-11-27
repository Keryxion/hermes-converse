"""Countdowns: the ``countdown`` agent tool, the device's countdown list, and still-ringing turns.

The device keeps the time and rings. The server records each device's countdowns (in memory only,
so a server restart forgets them), relays them, and resends them after ``ready``. The device reports
each countdown that rings, and each that ends, with its own ``countdown`` frame. While any rings, the
server starts one turn in the device's conversation at 1, 2, 4 and 8 minutes after the earliest deadline
among them, naming every one ringing. It sends no frame at the deadline and listens for no stop
phrase.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .voice_tools import TOOLSET

logger = logging.getLogger(__name__)

SOUNDS = ("alarm-1", "alarm-2", "alarm-3", "alarm-4")
STILL_RINGING_SECONDS = (60, 120, 240, 480)

SCHEMA = {
    "name": "countdown",
    "description": (
        "Voice devices only. Any timer or alarm the person asks for; it rings on this device. "
        "set: at (ISO 8601) or in_seconds, optional sound and label; returns countdown_id. "
        "snooze: new time for countdown_id, also while ringing. cancel: end countdown_id. "
        "list: this device's countdowns."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["set", "snooze", "cancel", "list"]},
            "at": {"type": "string", "description": "Future time, ISO 8601."},
            "in_seconds": {"type": "number", "description": "Seconds from now."},
            "sound": {"type": "string", "enum": list(SOUNDS)},
            "label": {"type": "string"},
            "countdown_id": {"type": "string"},
        },
        "required": ["action"],
    },
}


@dataclass
class _Countdown:
    countdown_id: str
    deadline: float  # unix seconds
    sound: str
    label: Optional[str]
    generation: int = 0  # bumped by snooze so older expiry timers do nothing
    timers: list = field(default_factory=list)
    ringing_since: Optional[float] = None  # its deadline, once the device reports it ringing


@dataclass
class _Device:
    source: Any = None
    loop: Optional[asyncio.AbstractEventLoop] = None
    countdowns: dict = field(default_factory=dict)
    ring_timers: list = field(default_factory=list)  # one still-ringing sequence for the device
    ring_generation: int = 0


def _deadline(args: dict, now: float) -> Optional[float]:
    seconds = args.get("in_seconds")
    at = args.get("at")
    try:
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
            deadline = now + float(seconds)
        elif isinstance(at, str) and at.strip():
            deadline = datetime.fromisoformat(at.strip().replace("Z", "+00:00")).timestamp()
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    return deadline if deadline > now else None


def _result(value: str) -> str:
    return json.dumps({"result": value})


class Countdowns:
    def __init__(self, hub: Any) -> None:
        self._hub = hub
        self._lock = threading.RLock()
        self._devices: dict[str, _Device] = {}  # conversation_id -> its countdowns
        self.ring_seconds = 900

    # ── device side (called from the socket) ──
    def attached(self, attachment: Any, source: Any, ring_seconds: float,
                 loop: asyncio.AbstractEventLoop) -> None:
        """The device is connected and has sent ``ready``: send it its countdowns again."""
        with self._lock:
            self.ring_seconds = int(ring_seconds)
            device = self._devices.setdefault(attachment.conversation_id, _Device())
            device.source, device.loop = source, loop
            self._purge(device)
            for item in device.countdowns.values():
                self._send_countdown(attachment, item)

    def reported(self, conversation_id: str, frame: dict) -> None:
        """The device sent ``countdown``: one of its countdowns is ringing, or has ended."""
        countdown_id = frame.get("countdown_id")
        if not isinstance(countdown_id, str):
            return
        with self._lock:
            device = self._devices.get(conversation_id)
            if device is None:
                return
            if frame.get("ended") is True:
                self._remove(device, countdown_id)
            else:
                item = device.countdowns.get(countdown_id)
                if item is None or frame.get("ringing") is not True:
                    return
                item.ringing_since = item.deadline  # it rings at its deadline, never late
            self._schedule_ringing(conversation_id, device)

    # ── tool ──
    def handle(self, args: dict, **kw: Any) -> str:
        from gateway.session_context import get_session_env

        if get_session_env("HERMES_SESSION_PLATFORM", "") != "converse":
            return _result("not_a_device")
        conversation_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
        attachment = self._hub.attachment(conversation_id)
        if attachment is None or attachment.send is None:
            return _result("not_attached")
        action = args.get("action")
        with self._lock:
            device = self._devices.setdefault(conversation_id, _Device())
            self._purge(device)
            if action == "list":
                now = time.time()
                return json.dumps({"countdowns": [
                    {"countdown_id": c.countdown_id, "label": c.label,
                     "deadline": datetime.fromtimestamp(c.deadline).astimezone().isoformat(timespec="seconds"),
                     "deadline_unix_ms": int(c.deadline * 1000),
                     "state": "pending" if c.deadline > now else "ringing"}
                    for c in device.countdowns.values()]})
            if action == "set":
                deadline = _deadline(args, time.time())
                if deadline is None:
                    return _result("bad_time")
                sound = args.get("sound") if args.get("sound") in SOUNDS else "alarm-1"
                label = args.get("label") if isinstance(args.get("label"), str) and args["label"].strip() else None
                item = _Countdown(f"countdown-{uuid.uuid4().hex[:8]}", deadline, sound, label)
                device.countdowns[item.countdown_id] = item
                self._send_countdown(attachment, item)
                self._schedule(conversation_id, item)
                return json.dumps({"result": "set", "countdown_id": item.countdown_id,
                                   "deadline_unix_ms": int(deadline * 1000)})
            if action not in ("snooze", "cancel"):
                return json.dumps({"error": "action must be set, snooze, cancel or list"})
            item = device.countdowns.get(args.get("countdown_id"))
            if item is None:
                return _result("unknown_countdown")
            if action == "cancel":
                self._remove(device, item.countdown_id)
                self._schedule_ringing(conversation_id, device)
                attachment.send({"type": "countdown_cancel", "countdown_id": item.countdown_id})
                return _result("ok")
            deadline = _deadline(args, time.time())
            if deadline is None:
                return _result("bad_time")
            self._clear_timers(item)
            item.deadline = deadline
            item.generation += 1
            item.ringing_since = None  # counting again; the device reports when it next rings
            self._send_countdown(attachment, item)
            self._schedule(conversation_id, item)
            self._schedule_ringing(conversation_id, device)
            return json.dumps({"result": "ok", "countdown_id": item.countdown_id,
                               "deadline_unix_ms": int(deadline * 1000)})

    # ── internals ──
    def _send_countdown(self, attachment: Any, item: _Countdown) -> None:
        if attachment.send is None:
            return
        frame = {"type": "countdown", "countdown_id": item.countdown_id,
                 "deadline_unix_ms": int(item.deadline * 1000), "ring_seconds": self.ring_seconds,
                 "sound": item.sound}
        if item.label:
            frame["label"] = item.label
        attachment.send(frame)

    def _purge(self, device: _Device) -> None:
        now = time.time()
        for countdown_id in [i for i, c in device.countdowns.items() if c.deadline + self.ring_seconds <= now]:
            self._remove(device, countdown_id)

    @staticmethod
    def _clear_timers(item: _Countdown) -> None:
        for timer in item.timers:
            timer.cancel()
        item.timers = []

    def _remove(self, device: _Device, countdown_id: str) -> None:
        item = device.countdowns.pop(countdown_id, None)
        if item is not None:
            self._clear_timers(item)

    def _schedule(self, conversation_id: str, item: _Countdown) -> None:
        """A timer for dropping the countdown after the ring timeout."""
        generation = item.generation
        self._timer(item.timers, item.deadline + self.ring_seconds - time.time(),
                    lambda: self._expire(conversation_id, item, generation))

    def _schedule_ringing(self, conversation_id: str, device: _Device) -> None:
        """One still-ringing sequence for the device, timed from the earliest deadline among the
        countdowns it reports ringing. Called whenever what rings changes."""
        for timer in device.ring_timers:
            timer.cancel()
        device.ring_timers = []
        device.ring_generation += 1
        starts = [c.ringing_since for c in device.countdowns.values() if c.ringing_since is not None]
        if not starts:
            return
        first, generation, now = min(starts), device.ring_generation, time.time()
        for offset in STILL_RINGING_SECONDS:
            if offset >= self.ring_seconds:
                break
            if first + offset > now:
                self._timer(device.ring_timers, first + offset - now,
                            lambda: self._still_ringing(conversation_id, generation))

    @staticmethod
    def _timer(timers: list, delay: float, fn: Any) -> None:
        timer = threading.Timer(max(0.0, delay), fn)
        timer.daemon = True
        timers.append(timer)
        timer.start()

    def _expire(self, conversation_id: str, item: _Countdown, generation: int) -> None:
        with self._lock:
            device = self._devices.get(conversation_id)
            if device is not None and device.countdowns.get(item.countdown_id) is item \
                    and item.generation == generation:
                self._remove(device, item.countdown_id)
                self._schedule_ringing(conversation_id, device)

    def _still_ringing(self, conversation_id: str, generation: int) -> None:
        with self._lock:
            device = self._devices.get(conversation_id)
            if device is None or device.ring_generation != generation:
                return
            now = time.time()
            ringing = sorted((c for c in device.countdowns.values() if c.ringing_since is not None),
                             key=lambda c: c.ringing_since)
            source, loop = device.source, device.loop
        adapter = self._hub.gateway
        if not ringing or source is None or loop is None or adapter is None \
                or self._hub.attachment(conversation_id) is None:
            return  # nothing ringing, or no device to speak on
        from gateway.platforms.event import MessageEvent, MessageType

        def ringing_for(item: _Countdown) -> str:
            minutes = int(now - item.ringing_since) // 60
            label = f' "{item.label}"' if item.label else ""
            return f"{item.countdown_id}{label} for {minutes} minute{'s' if minutes != 1 else ''}"

        if len(ringing) == 1:
            text = (f"[Countdown {ringing_for(ringing[0])} has been ringing on this device and nobody "
                    "has ended it. End it with the countdown tool (cancel or snooze), or leave it ringing.]")
        else:
            text = ("[Countdowns ringing on this device that nobody has ended: "
                    + "; ".join(ringing_for(c) for c in ringing)
                    + ". End each with the countdown tool (cancel or snooze), or leave them ringing.]")
        event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source,
                             message_id=f"countdown-ring-{uuid.uuid4().hex}")
        future = asyncio.run_coroutine_threadsafe(adapter.handle_message(event), loop)
        future.add_done_callback(lambda f: f.exception() and logger.warning(
            "converse: still-ringing turn failed: %s", f.exception()))


def register_tool(ctx: Any, hub: Any) -> None:
    hub.countdowns = Countdowns(hub)
    ctx.register_tool(name="countdown", toolset=TOOLSET, schema=SCHEMA, handler=hub.countdowns.handle,
                      description=SCHEMA["description"], emoji="⏲️")
