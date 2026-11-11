"""The agent turn over the public Runs API, on loopback.

One :class:`RunsClient` per WebSocket connection. A turn is ``POST /v1/runs`` (with the socket's
``session_id`` so the server continues the same conversation, the voice ``instructions``, and an
``Idempotency-Key`` so a retried POST can't start a second turn) followed by the
``/events`` SSE stream. The rules the stream reader follows, each guarding a real hazard:

* Speak only ``message.delta``. ``message.interim`` repeats text the deltas already carried
  (``already_streamed``) and ``run.completed`` carries the whole reply again; both are read for the
  history, never spoken.
* Track the sequence number of every event; reconnect with ``Last-Event-ID`` and drop anything
  already seen, so a replay can never double-speak.
* A dropped SSE stream only detaches a subscriber: the run keeps going. Barge-in, the turn deadline
  and the socket closing all ``POST /stop`` and then wait for the terminal event (bounded), so a
  turn is never orphaned on the server.
* An ``approval.request`` or ``clarify.request`` would otherwise hang the voice turn; they are
  answered at once with the configured choice (deny by default) or cancelled.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)

TERMINAL_EVENTS = {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
# The events endpoint waits only ~1 s for a run to register; if it 404s right after the POST,
# poll the status instead of giving up.
SUBSCRIBE_RETRIES = 3
RECONNECT_ATTEMPTS = 3
POLL_INTERVAL = 0.5


class RunsError(Exception):
    """The API server refused or lost a run; the message is what the voice turn reports."""


@dataclass
class TurnResult:
    reply: str
    error: Optional[str]
    status: str
    run_id: Optional[str]


def _sse_events(raw_lines):
    """Parse SSE lines into ``(seq, payload_dict)``; comments and blank frames are skipped."""
    seq: Optional[int] = None
    data: list[str] = []
    for line in raw_lines:
        if line == "":
            if data:
                try:
                    yield seq, json.loads("\n".join(data))
                except ValueError:
                    logger.debug("converse: unparsable SSE frame dropped")
            seq, data = None, []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "id":
            with contextlib.suppress(ValueError):
                seq = int(value.strip())
        elif field == "data":
            data.append(value)


class RunsClient:
    """Run voice turns through ``/v1/runs`` with the client's own API key."""

    def __init__(self, base_url: str, api_key: str, *, http: aiohttp.ClientSession,
                 approval_choice: str = "deny", settle_timeout: float = 10.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._http = http
        self._approval_choice = approval_choice
        self._settle_timeout = settle_timeout
        self._run_id: Optional[str] = None
        self._stream_task: Optional[asyncio.Task] = None
        self._stop_requested = False
        self._stops_posted: set[str] = set()
        # Keep admission/stop uncertainty visible until a public terminal status confirms it.
        self.unsettled_run_ids: set[str] = set()
        self.admission_uncertain = False
        self.tool_calls = 0  # Observed tool starts for the most recent run; retained through TTS.

    @property
    def current_run_id(self) -> Optional[str]:
        return self._run_id

    async def model_options(self) -> dict[str, Any]:
        """Reuse the authenticated server inventory behind the voice WebSocket."""
        params = {"include_unconfigured": "false", "refresh": "false"}
        async with asyncio.timeout(8):
            async with self._http.get(f"{self._base}/api/model/options", params=params,
                                      headers=self._headers, allow_redirects=False) as response:
                if response.status != 200:
                    raise RunsError('model_catalog_unavailable')
                result = await response.json()
                if not isinstance(result, dict) or not isinstance(result.get('providers'), list):
                    raise RunsError('model_catalog_unavailable')
                return result

    # ── turn ──
    async def run_turn(self, *, text: str, session_id: str, on_delta: Callable[[str], None],
                       instructions: Optional[str] = None, model: Optional[str] = None,
                       provider: Optional[str] = None, idempotency_key: Optional[str] = None) -> TurnResult:
        """Start a run and stream it to completion. Returns the reply for the history and the error
        (if any) the client should hear about; ``status`` is the run's terminal status."""
        self.tool_calls = 0
        if self.admission_uncertain:
            return TurnResult("", "Previous request admission is unresolved; reconnect before retrying.", "failed", None)
        for previous in tuple(self.unsettled_run_ids):
            await self._settle_by_polling(previous, on_delta_seen=True, reason="previous run still stopping")
        if self.unsettled_run_ids:
            return TurnResult("", "Previous request is still stopping; please try again.", "failed", None)
        body: dict[str, Any] = {"input": text, "session_id": session_id}
        if instructions:
            body["instructions"] = instructions
        if model:
            body["model"] = model
        if provider:
            body["provider"] = provider
        admission = asyncio.ensure_future(self._post_run(body, idempotency_key))
        try:
            run_id = await asyncio.shield(admission)
        except asyncio.CancelledError:
            # An accepted POST may not have returned its id yet. Cancelling the HTTP
            # request here would orphan the accepted run, so finish admission and stop it.
            async def cancel_admission():
                try:
                    accepted_id = await admission
                except (RunsError, TimeoutError, aiohttp.ClientError):
                    return
                await self._post_stop(accepted_id)
            await self._finish_cleanup(cancel_admission())
            raise
        except RunsError as exc:
            return TurnResult("", str(exc), "failed", None)
        self._run_id, self._stop_requested = run_id, False
        task = self._stream_task = asyncio.ensure_future(self._stream(run_id, on_delta))
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                # The host abandoned the turn (deadline / socket closed): never leave it running.
                await self._finish_cleanup(self._abort(run_id, task))
                raise
            # stop() cancelled the stream after its settle timeout: the turn is over for us.
            return TurnResult("", None, "interrupted", run_id)
        finally:
            self._run_id, self._stream_task = None, None

    async def stop(self) -> None:
        """Interrupt the current run (barge-in, socket close) and wait — bounded — for it to settle."""
        run_id, task = self._run_id, self._stream_task
        if run_id is None:
            return
        self._stop_requested = True
        await self._post_stop(run_id)
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), self._settle_timeout)
            except TimeoutError:
                logger.warning("converse: run %s did not settle after stop; detaching", run_id)
                task.cancel()
            except Exception:
                pass

    # ── HTTP ──
    async def _post_run(self, body: dict[str, Any], idempotency_key: Optional[str]) -> str:
        self.admission_uncertain = True
        headers = dict(self._headers)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        last_exc: Optional[Exception] = None
        for attempt in range(2):  # the Idempotency-Key makes a retry safe
            try:
                async with self._http.post(f"{self._base}/v1/runs", json=body, headers=headers,
                                           allow_redirects=False) as resp:
                    payload = await self._json(resp)
                    if resp.status in (200, 202) and isinstance(payload, dict) and payload.get("run_id"):
                        run_id = str(payload["run_id"])
                        self.unsettled_run_ids.add(run_id)
                        self.admission_uncertain = False
                        return run_id
                    if 400 <= resp.status < 500:
                        self.admission_uncertain = False
                    raise RunsError(self._error_text(resp.status, payload))
            except aiohttp.ClientConnectionError as exc:
                last_exc = exc
                if attempt == 0 and idempotency_key:
                    continue
                raise RunsError(f"agent API unreachable: {exc}") from exc
        raise RunsError(f"agent API unreachable: {last_exc}")

    async def _post_stop(self, run_id: str) -> None:
        if run_id in self._stops_posted:
            return  # a barge-in followed by the socket closing stops the same run once
        self._stops_posted.add(run_id)
        with contextlib.suppress(Exception):
            async with self._http.post(f"{self._base}/v1/runs/{run_id}/stop", headers=self._headers,
                                       allow_redirects=False, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                await resp.read()

    async def _finish_cleanup(self, cleanup) -> None:
        """A second cancel/close must not interrupt the bounded HTTP stop cleanup."""
        task = asyncio.ensure_future(cleanup)
        while True:
            try:
                await asyncio.shield(task)
                return
            except asyncio.CancelledError:
                if task.cancelled():
                    raise

    async def _abort(self, run_id: str, task: asyncio.Task) -> None:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        await self._post_stop(run_id)

    async def _status(self, run_id: str) -> Optional[dict[str, Any]]:
        with contextlib.suppress(Exception):
            async with self._http.get(f"{self._base}/v1/runs/{run_id}", headers=self._headers,
                                      allow_redirects=False, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                payload = await self._json(resp)
                if resp.status == 200 and isinstance(payload, dict):
                    return payload
        return None

    async def _answer(self, run_id: str, kind: str, event: dict[str, Any]) -> None:
        """Resolve a blocking prompt so the voice turn never hangs on it."""
        body: dict[str, Any] = {"request_id": event.get("request_id")}
        if "execution_generation" in event:
            body["execution_generation"] = event["execution_generation"]
        if kind == "approval":
            body["choice"] = self._approval_choice
        else:
            body["answer"] = {}  # cancel: a voice turn cannot answer a clarify form
        try:
            async with self._http.post(f"{self._base}/v1/runs/{run_id}/{kind}", json=body,
                                       headers=self._headers, allow_redirects=False,
                                       timeout=aiohttp.ClientTimeout(total=5)) as resp:
                await resp.read()
                if resp.status >= 300:
                    raise RunsError(f"{kind} answer refused ({resp.status})")
        except Exception as exc:
            logger.warning("converse: could not answer %s for run %s (%s); stopping it", kind, run_id, exc)
            await self._post_stop(run_id)

    @staticmethod
    async def _json(resp: aiohttp.ClientResponse) -> Any:
        try:
            return await resp.json(content_type=None)
        except Exception:
            return None

    @staticmethod
    def _error_text(status: int, payload: Any) -> str:
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict) and err.get("message"):
                return f"agent API refused the turn ({status}): {err['message']}"
        return f"agent API refused the turn ({status})"

    # ── SSE ──
    async def _stream(self, run_id: str, on_delta: Callable[[str], None]) -> TurnResult:
        last_seq = -1
        attempts = 0
        while True:
            headers = dict(self._headers)
            if last_seq >= 0:
                headers["Last-Event-ID"] = str(last_seq)
            try:
                async with self._http.get(f"{self._base}/v1/runs/{run_id}/events", headers=headers,
                                          allow_redirects=False) as resp:
                    if resp.status == 404 and attempts < SUBSCRIBE_RETRIES:
                        attempts += 1
                        await asyncio.sleep(0.3)
                        continue
                    if resp.status != 200:
                        return await self._settle_by_polling(run_id, on_delta_seen=last_seq >= 0,
                                                             reason=f"event stream refused ({resp.status})")
                    async for seq, event in self._iter_events(resp):
                        if seq is not None:
                            if seq <= last_seq:
                                continue  # a replay of something already spoken
                            last_seq = seq
                        outcome = await self._dispatch(run_id, event, on_delta)
                        if outcome is not None:
                            return outcome
            except (aiohttp.ClientError, asyncio.IncompleteReadError, TimeoutError) as exc:
                logger.debug("converse: event stream for %s dropped: %s", run_id, exc)
            # Stream ended without a terminal event: the run may still be live.
            status = await self._status(run_id)
            if status is not None and status.get("status") in TERMINAL_STATUSES:
                return self._from_status(status)
            if self._stop_requested:
                return await self._settle_by_polling(run_id, on_delta_seen=True, reason="stopped")
            attempts += 1
            if attempts > RECONNECT_ATTEMPTS:
                return await self._settle_by_polling(run_id, on_delta_seen=True, reason="event stream lost")
            await asyncio.sleep(0.3)

    async def _iter_events(self, resp: aiohttp.ClientResponse):
        async def _lines():
            async for raw in resp.content:
                yield raw.decode("utf-8", "replace").rstrip("\r\n")
            yield ""  # flush a frame without a trailing blank line

        buf: list[str] = []
        async for line in _lines():
            buf.append(line)
            if line == "":
                for item in _sse_events(buf):
                    yield item
                buf = []

    async def _dispatch(self, run_id: str, event: dict[str, Any], on_delta: Callable[[str], None],
                        ) -> Optional[TurnResult]:
        name = event.get("event")
        if name == "tool.started":
            self.tool_calls += 1
            return None
        if name == "message.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                on_delta(delta)
            return None
        if name == "approval.request":
            await self._answer(run_id, "approval", event)
            return None
        if name == "clarify.request":
            await self._answer(run_id, "clarify", event)
            return None
        if name == "run.completed":
            self.unsettled_run_ids.discard(run_id)
            return TurnResult(str(event.get("output") or ""), None, "completed", run_id)
        if name == "run.failed":
            self.unsettled_run_ids.discard(run_id)
            return TurnResult("", str(event.get("error") or "agent run failed"), "failed", run_id)
        if name in ("run.cancelled", "run.interrupted"):
            self.unsettled_run_ids.discard(run_id)
            error = None if self._stop_requested else str(event.get("error") or "agent run was cancelled")
            return TurnResult(str(event.get("output") or ""), error, name.split(".", 1)[1], run_id)
        return None  # message.interim, other tool.*, reasoning.*, replay.truncated, run.steered, ...

    def _from_status(self, status: dict[str, Any]) -> TurnResult:
        state = str(status.get("status") or "")
        run_id = status.get("run_id")
        if state in TERMINAL_STATUSES:
            self.unsettled_run_ids.discard(run_id)
        if state == "completed":
            return TurnResult(str(status.get("output") or ""), None, state, run_id)
        if state == "failed":
            return TurnResult("", str(status.get("error") or "agent run failed"), state, run_id)
        error = None if self._stop_requested else str(status.get("error") or "agent run was cancelled")
        return TurnResult(str(status.get("output") or ""), error, state or "cancelled", run_id)

    async def _settle_by_polling(self, run_id: str, *, on_delta_seen: bool, reason: str) -> TurnResult:
        """The stream is gone; poll the status until the run settles (bounded) so the turn ends."""
        deadline = asyncio.get_running_loop().time() + self._settle_timeout
        while True:
            status = await self._status(run_id)
            if status is not None and status.get("status") in TERMINAL_STATUSES:
                return self._from_status(status)
            if asyncio.get_running_loop().time() >= deadline:
                if not self._stop_requested:
                    await self._post_stop(run_id)
                self.unsettled_run_ids.discard(run_id)
                return TurnResult("", None if self._stop_requested else reason, "interrupted", run_id)
            await asyncio.sleep(POLL_INTERVAL)
