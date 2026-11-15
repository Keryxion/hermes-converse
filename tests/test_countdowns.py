"""The device reports its countdowns: ringing and ended. Still-ringing turns follow
those reports, one sequence per device however many ring."""

from __future__ import annotations

import asyncio
import threading
import time
import types

from hermes_converse import countdowns as cd


def _rig(*items):
    hub = types.SimpleNamespace(gateway=None, attachment=lambda cid: object())
    c = cd.Countdowns(hub)
    device = c._devices.setdefault("chat", cd._Device())
    for item in items:
        device.countdowns[item.countdown_id] = item
    return c, device, hub


def _item(cid, label=None, rang=1):
    return cd._Countdown(cid, time.time() - rang, "alarm-1", label)


def test_ended_report_forgets_the_countdown_and_its_still_ringing_turns():
    c, device, _ = _rig(_item("countdown-1"))
    c.reported("chat", {"type": "countdown", "countdown_id": "countdown-1", "ringing": True})
    assert device.ring_timers
    c.reported("chat", {"type": "countdown", "countdown_id": "countdown-1", "ended": True})
    assert "countdown-1" not in device.countdowns and device.ring_timers == []


def test_a_reconnect_while_ringing_times_turns_from_the_deadline():
    c, device, _ = _rig(_item("countdown-1", rang=90))
    c.reported("chat", {"type": "countdown", "countdown_id": "countdown-1", "ringing": True})
    item = device.countdowns["countdown-1"]
    assert item.ringing_since == item.deadline
    # 60 s has passed already: only the 120, 240 and 480 s turns remain.
    assert len(device.ring_timers) == 3
    for timer in device.ring_timers:
        timer.cancel()


def test_bad_reports_change_nothing():
    c, device, _ = _rig(_item("countdown-1"))
    for frame in ({"countdown_id": 7, "ringing": True}, {"countdown_id": "countdown-1"},
                  {"countdown_id": "countdown-1", "ringing": "yes"},
                  {"countdown_id": "unknown", "ringing": True}):
        c.reported("chat", {"type": "countdown", **frame})
    assert device.countdowns["countdown-1"].ringing_since is None and device.ring_timers == []


def test_two_ringing_share_one_sequence_and_one_turn_names_both(monkeypatch):
    first, second = _item("countdown-1", "tea", rang=130), _item("countdown-2", "eggs", rang=10)
    c, device, hub = _rig(first, second)
    for cid in ("countdown-1", "countdown-2"):
        c.reported("chat", {"type": "countdown", "countdown_id": cid, "ringing": True})
    assert len(device.ring_timers) == 2  # anchored on the earliest deadline: 240 s and 480 s remain
    for timer in device.ring_timers:
        timer.cancel()

    sent = []
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def handle_message(event):
        sent.append(event.text)

    hub.gateway = types.SimpleNamespace(handle_message=handle_message)
    device.source, device.loop = object(), loop
    try:
        c._still_ringing("chat", device.ring_generation)
        deadline = time.time() + 2
        while not sent and time.time() < deadline:
            time.sleep(0.01)
    finally:
        loop.call_soon_threadsafe(loop.stop)
    (text,) = sent
    assert 'countdown-1 "tea" for 2 minutes' in text and 'countdown-2 "eggs" for 0 minutes' in text


def test_a_stale_sequence_starts_no_turn():
    c, device, hub = _rig(_item("countdown-1"))
    c.reported("chat", {"type": "countdown", "countdown_id": "countdown-1", "ringing": True})
    stale = device.ring_generation
    c.reported("chat", {"type": "countdown", "countdown_id": "countdown-1", "ended": True})
    hub.gateway = types.SimpleNamespace(handle_message=None)  # would fail if reached
    c._still_ringing("chat", stale)
