"""Fresh diagnostics requested from one socket; no cache or reconnect handoff."""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from aiohttp import ClientError

TIMEOUT = 5.0
MAX_BYTES = 32 * 1024


class ClientDiagnostics:
    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self._closed = False
        self._pending: dict[str, tuple[asyncio.Future, float]] = {}

    async def request(self) -> dict:
        if self._closed or self.ws.closed:
            return {"error": "disconnected"}
        loop = asyncio.get_running_loop()
        request_id = uuid.uuid4().hex
        answer = loop.create_future()
        deadline = loop.time() + TIMEOUT
        self._pending[request_id] = answer, deadline
        try:
            async with asyncio.timeout_at(deadline):
                await self.ws.send_json({"type": "client_diagnostics", "request_id": request_id})
                return await answer
        except TimeoutError:
            return {"error": "timeout"}
        except (ConnectionError, ClientError, RuntimeError):
            self.close()
            return {"error": "disconnected"}
        finally:
            self._pending.pop(request_id, None)
            if not answer.done():
                answer.cancel()

    def respond(self, frame: dict, raw: str) -> None:
        request_id = frame.get("request_id")
        if not isinstance(request_id, str):
            return
        pending = self._pending.get(request_id)
        if pending is None:
            return
        answer, deadline = pending
        if answer.done() or asyncio.get_running_loop().time() >= deadline:
            return
        try:
            result = _snapshot(frame) if len(raw.encode("utf-8")) <= MAX_BYTES else None
        except (ValueError, RecursionError, UnicodeError):
            result = None
        answer.set_result(result if result is not None else {"error": "invalid_response"})

    def close(self) -> None:
        self._closed = True
        for answer, _ in self._pending.values():
            if not answer.done():
                answer.set_result({"error": "disconnected"})


def _snapshot(frame: dict) -> dict | None:
    if "error" in frame:
        error = frame["error"]
        return {"error": error} if isinstance(error, str) and error.strip() else None
    if any(not isinstance(frame.get(key), str) or not frame[key].strip() for key in ("version", "build")):
        return None
    if not isinstance(frame.get("diagnostics"), dict):
        return None
    result = {key: frame[key] for key in ("version", "build", "diagnostics")}
    if "platform" in frame:
        if not isinstance(frame["platform"], str) or not frame["platform"].strip():
            return None
        result["platform"] = frame["platform"]
    # Python's JSON decoder accepts NaN/Infinity; they are not JSON measurements.
    json.dumps(result, allow_nan=False)
    return result
