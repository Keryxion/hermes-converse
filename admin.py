"""``GET /v1/audio/admin``: one WebSocket for software that manages the devices (specs/admin.md).

An admin connection is sent every device and each change to one, and can announce on devices
and set their mute and volume. A device's mute and volume are the device's own last reports,
kept only while it is attached.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from aiohttp import ClientSession, ClientTimeout, web

from .auth import verify_api_key
from .named import DeviceStore, normalize_name

logger = logging.getLogger(__name__)

ROUTE_PATH = "/v1/audio/admin"
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
    volume: Optional[int] = None
    waiters: dict[str, list[asyncio.Future]] = field(default_factory=lambda: {"muted": [], "volume": []})


def _report(frame: dict) -> Optional[tuple[str, Any]]:
    """A device's ``mute`` or ``volume`` frame as (field, value); None when it is malformed."""
    if frame.get("type") == "mute" and isinstance(frame.get("on"), bool):
        return "muted", frame["on"]
    if frame.get("type") == "volume" and _is_level(frame.get("level")):
        return "volume", frame["level"]
    return None


def _is_level(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100


class Admin:
    def __init__(self, devices: DeviceStore, hub: Any, base_url: Callable[[], str]) -> None:
        self._devices, self._hub, self._base_url = devices, hub, base_url
        self._attached: dict[str, _Attached] = {}  # device_name -> its voice connection
        self._sockets: set[web.WebSocketResponse] = set()
        self._tasks: set[asyncio.Task] = set()

    # ── the voice route's side ──

    def attach(self, device_name: str, attachment: Any, ws: Any) -> None:
        self._attached[device_name] = _Attached(attachment, ws)
        self._changed(device_name)

    def detach(self, device_name: str, attachment: Any) -> None:
        entry = self._attached.get(device_name)
        if entry is not None and entry.attachment is attachment:
            del self._attached[device_name]
            self._settle(entry, "muted", "volume")
            self._changed(device_name)

    def reported(self, device_name: str, attachment: Any, frame: dict) -> None:
        """A ``mute`` or ``volume`` frame from the device."""
        entry, report = self._attached.get(device_name), _report(frame)
        if entry is None or entry.attachment is not attachment or report is None:
            return
        key, value = report
        changed = getattr(entry, key) != value
        setattr(entry, key, value)
        self._settle(entry, key)
        if changed:
            self._changed(device_name)

    @staticmethod
    def _settle(entry: _Attached, *keys: str) -> None:
        for key in keys:
            for waiter in entry.waiters[key]:
                if not waiter.done():
                    waiter.set_result(None)
            entry.waiters[key].clear()

    # ── frames to admin connections ──

    def _device(self, device_name: str) -> dict:
        entry = self._attached.get(device_name)
        return {"device_name": device_name, "attached": entry is not None,
                "muted": entry.muted if entry else None, "volume": entry.volume if entry else None}

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
        names = await asyncio.to_thread(self._devices.names)
        # No await between the list and joining the set: every later change follows it.
        self._send(ws, {"type": "devices",
                        "devices": [self._device(name) for name in sorted({*names, *self._attached})]})
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
            known = name in await asyncio.to_thread(self._devices.names)
            return {"error": "not_attached" if known else "unknown_device"}
        waiters = []
        for key, value in wanted.items():
            waiter = asyncio.get_running_loop().create_future()
            entry.waiters[key].append(waiter)
            waiters.append(waiter)
            with contextlib.suppress(Exception):  # a socket that just closed: the device detaches
                await entry.ws.send_json({"type": "mute", "on": value} if key == "muted"
                                         else {"type": "volume", "level": value})
        await asyncio.wait(waiters, timeout=SET_TIMEOUT)
        for key in wanted:
            entry.waiters[key] = [waiter for waiter in entry.waiters[key] if waiter not in waiters]
        return {"device": self._device(name)}
