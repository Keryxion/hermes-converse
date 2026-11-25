"""``GET /v1/audio/admin``: one WebSocket for software that manages the devices (specs/admin.md).

An admin connection is sent every device and each change to one, and can announce on devices
and set their mute and volume. A device's mute and volume are the device's own last reports,
kept only while it is attached.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import ipaddress
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from aiohttp import ClientSession, ClientTimeout, web

from .auth import verify_api_key
from .named import DeviceStore, normalize_name

logger = logging.getLogger(__name__)

ROUTE_PATH = "/v1/audio/admin"
LISTED_SECONDS = 30 * 86400  # a device unattached this long leaves the list and is removed
PURGE_INTERVAL = 3600.0  # seconds between removals of devices that have ended
SET_TIMEOUT = 2.0  # seconds a `set` waits for the device's report
HTTP_TIMEOUT = ClientTimeout(total=30, connect=5)


class BadRequest(Exception):
    pass


@dataclass(eq=False)
class _Attached:
    """One attached device: its hub attachment, voice socket, and what it last reported."""
    attachment: Any
    ws: Any
    muted: Optional[bool] = None
    volume: Optional[float] = None
    waiters: list[asyncio.Future] = field(default_factory=list)  # `set` requests awaiting its next report


def _report(frame: dict) -> dict:
    """The valid fields of a device's ``controls`` frame."""
    report = {}
    if isinstance(frame.get("muted"), bool):
        report["muted"] = frame["muted"]
    if _is_level(frame.get("volume")):
        report["volume"] = frame["volume"]
    return report


def _is_level(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1


def client_address(request: web.Request) -> Optional[str]:
    """The IP address a connection came from: the first of ``X-Forwarded-For``, else the peer."""
    for candidate in (request.headers.get("X-Forwarded-For", "").split(",")[0].strip(), request.remote):
        with contextlib.suppress(ValueError):
            return str(ipaddress.ip_address(candidate or ""))
    return None


class Admin:
    def __init__(self, devices: DeviceStore, hub: Any, base_url: Callable[[], str]) -> None:
        self._devices, self._hub, self._base_url = devices, hub, base_url
        self._attached: dict[str, _Attached] = {}  # device_name -> its voice connection
        self._seen: dict[str, dict] = {}  # device_name -> its stored last_attached and address
        self._sockets: set[web.WebSocketResponse] = set()
        self._tasks: set[asyncio.Task] = set()
        self._purger: Optional[asyncio.Task] = None  # the hourly removal

    # ── the voice route's side ──

    def attach(self, device_name: str, attachment: Any, ws: Any, address: Optional[str]) -> None:
        self._attached[device_name] = _Attached(attachment, ws)
        self._seen[device_name] = {"address": address}
        self._record(device_name)
        self._changed(device_name)

    def _record(self, device_name: str) -> None:
        """Keep when the device was last attached, and from where, for while it is away."""
        seen = self._seen.setdefault(device_name, {})
        seen["last_attached"] = time.time()

        async def record() -> None:
            with contextlib.suppress(Exception):  # the store closes at shutdown
                await asyncio.to_thread(self._devices.update_settings, device_name, **seen)
        self._spawn(record())

    def _listed(self, device_name: str) -> bool:
        """Attached now, or within the last 30 days."""
        last = self._seen.get(device_name, {}).get("last_attached")
        return device_name in self._attached or (
            isinstance(last, (int, float)) and last >= time.time() - LISTED_SECONDS)

    def _devices_frame(self) -> dict:
        return {"type": "devices", "devices": [
            self._device(name) for name in sorted({*self._seen, *self._attached}) if self._listed(name)]}

    async def _load(self) -> None:
        """Learn the stored devices this process has not seen attach."""
        for name, settings in (await asyncio.to_thread(self._devices.settings)).items():
            self._seen.setdefault(name, settings)

    async def purge(self) -> None:
        """Remove the devices that have ended, and tell admin connections the list without them."""
        removed = await asyncio.to_thread(self._devices.remove_stale, time.time() - LISTED_SECONDS,
                                          set(self._attached))
        for name in removed:
            if name not in self._attached:
                self._seen.pop(name, None)
        if removed:
            logger.info("converse: removed devices unattached for 30 days: %s", removed)
            await self._load()
            for ws in self._sockets:
                self._send(ws, self._devices_frame())

    async def run_purge(self, app: Any = None) -> None:
        """Start the hourly removal; an ``on_startup`` hook."""
        async def hourly() -> None:
            while True:
                await asyncio.sleep(PURGE_INTERVAL)
                try:
                    await self.purge()
                except Exception:
                    logger.exception("converse: removing ended devices failed")
        self._purger = asyncio.ensure_future(hourly())

    async def close(self, app: Any = None) -> None:
        if self._purger is not None:
            self._purger.cancel()

    def detach(self, device_name: str, attachment: Any) -> None:
        entry = self._attached.get(device_name)
        if entry is not None and entry.attachment is attachment:
            del self._attached[device_name]
            self._settle(entry)
            self._record(device_name)
            self._changed(device_name)

    def reported(self, device_name: str, attachment: Any, frame: dict) -> None:
        """A ``controls`` frame from the device: its mute and volume as they now are."""
        entry = self._attached.get(device_name)
        if entry is None or entry.attachment is not attachment:
            return
        report = _report(frame)
        changed = any(getattr(entry, key) != value for key, value in report.items())
        for key, value in report.items():
            setattr(entry, key, value)
        self._settle(entry)
        if changed:
            self._changed(device_name)

    @staticmethod
    def _settle(entry: _Attached) -> None:
        for waiter in entry.waiters:
            if not waiter.done():
                waiter.set_result(None)
        entry.waiters.clear()

    # ── frames to admin connections ──

    def _device(self, device_name: str) -> dict:
        entry = self._attached.get(device_name)
        return {"device_name": device_name, "attached": entry is not None,
                "muted": entry.muted if entry else None, "volume": entry.volume if entry else None,
                "address": self._seen.get(device_name, {}).get("address")}

    def _changed(self, device_name: str) -> None:
        for ws in self._sockets:
            self._send(ws, {"type": "device", **self._device(device_name)})

    def _send(self, ws: web.WebSocketResponse, frame: dict) -> None:
        """Queue *frame* in call order without waiting on the socket: a slow admin connection
        never holds up a device's connection or another admin's."""
        async def send() -> None:
            with contextlib.suppress(Exception):
                await ws.send_json(frame)
        self._spawn(send())

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── the admin socket ──

    async def handle(self, request: web.Request) -> web.StreamResponse:
        scheme, _, key = request.headers.get("Authorization", "").partition(" ")
        async with ClientSession(timeout=HTTP_TIMEOUT) as http:
            if scheme.lower() != "bearer" or not await verify_api_key(self._base_url(), key.strip(), http=http):
                raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        await self._load()
        # No await between the list and joining the set: every later change follows it.
        self._send(ws, self._devices_frame())
        self._sockets.add(ws)
        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    frame = json.loads(msg.data)
                except (ValueError, TypeError):
                    continue
                if isinstance(frame, dict) and isinstance(frame.get("request_id"), str):
                    self._spawn(self._request(ws, frame))  # a `set` waiting on a device holds up nothing
        finally:
            self._sockets.discard(ws)
        return ws

    async def _request(self, ws: web.WebSocketResponse, frame: dict) -> None:
        try:
            handler = {"announce": self._announce, "set": self._set}.get(frame.get("type"))
            if handler is None:
                raise BadRequest
            outcome = await handler(frame)
        except BadRequest:
            outcome = {"error": "bad_request"}
        except Exception:
            logger.exception("converse admin: %s failed", frame.get("type"))
            return
        self._send(ws, {"type": "result", "request_id": frame["request_id"], **outcome})

    async def _announce(self, frame: dict) -> dict:
        message, target = frame.get("message"), frame.get("target", sorted(self._attached))
        if not isinstance(message, str) or not message.strip():
            raise BadRequest
        if isinstance(target, str):
            target = [target]
        if not isinstance(target, list) or not all(isinstance(name, str) for name in target):
            raise BadRequest
        announced, skipped = [], []
        for name in dict.fromkeys(normalize_name(name) for name in target):
            entry = self._attached.get(name)
            accepted = entry is not None and self._hub.announce(entry.attachment.conversation_id, message.strip())
            (announced if accepted else skipped).append(name)
        return {"announced": announced, "skipped": skipped}

    async def _set(self, frame: dict) -> dict:
        name = frame.get("device_name")
        wanted = {key: frame[key] for key in ("muted", "volume") if key in frame}
        if (not isinstance(name, str) or not wanted
                or not isinstance(wanted.get("muted", False), bool) or not _is_level(wanted.get("volume", 0))):
            raise BadRequest
        name = normalize_name(name)
        entry = self._attached.get(name)
        if entry is None:
            await self._load()
            return {"error": "not_attached" if self._listed(name) else "unknown_device"}
        waiter = asyncio.get_running_loop().create_future()
        entry.waiters.append(waiter)
        with contextlib.suppress(Exception):  # a socket that just closed: the device detaches
            await entry.ws.send_json({"type": "controls", **wanted})
        await asyncio.wait([waiter], timeout=SET_TIMEOUT)
        if waiter in entry.waiters:
            entry.waiters.remove(waiter)
        return {"device": self._device(name)}
