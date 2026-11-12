"""``GET /v1/audio/converse`` — realtime voice over one WebSocket, hosted on the API server.

Wired through ``ctx.register_platform_handler("api_server", ...)``: the factory receives the
aiohttp application and the adapter and adds the route. The client's FIRST frame is a single
``{"type":"start", ...}`` frame carrying auth + all session config; the key is presented one of
two ways (both verified against the API server itself, see :mod:`auth`):

(A) ``Sec-WebSocket-Protocol`` (browser clients): the client offers ``hermes-voice-v1`` plus
    ``hermes-key.<API_KEY>``. The key is verified BEFORE ``prepare`` and the socket is accepted
    selecting ONLY the base protocol (the key-bearing one is never echoed). A bad key rejects the
    upgrade with 401. ``start.key`` is then optional/ignored.
(B) ``start.key`` (non-browser devices, e.g. an ESP32): no subprotocol is offered, the socket is
    accepted, and the ``start`` frame's ``key`` authenticates. A non-``start`` first frame →
    ``{"error":"bad_start"}`` close 4400; a ``start`` with a bad/missing key →
    ``{"error":"unauthorized"}`` close 4401. No audio frame is processed until a valid start.

Browser-context clients must also pass the API server's own CORS origin guard (``cors_origins`` /
``API_SERVER_CORS_ORIGINS``), which runs before any route; an Electron renderer sends
``Origin: null``, so list it there.

Protocol:
  client → ``{"type":"start", "key":?, "input_rate":?, "output_rate":?, "quiet_interval":?,
             "name":?}`` then binary PCM16 mono frames at ``input_rate`` (30 ms blocks preferred),
           ``{"stop": true}`` to end, ``{"commit": true}`` to force an endpoint,
           ``{"type":"cancel","turn_id":?}`` to cancel the current turn and keep the session,
           ``{"type":"drained","turn_id":?}`` when the reply's last audio has left the speaker,
           ``{"type":"wake","claim_id":"...","wake_word":"...", ...}`` requests an
           advisory wake recommendation; see ``specs/client.md``. Accepted interruptions are captured as continuations.
  server → ``ready`` (rates, ``session_id``, clock-skew timestamps), ``transcript``, ``thinking``,
           ``speaking`` + binary PCM, ``interrupted``, ``turn_done`` (``next_action``), ``quiet``, ``error``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from .agent_client import RunsClient
from .arbitrate import Arbitrator
from .auth import offered_key_protocol, verify_api_key
from .conversation import ConversationHub
from .named import DeviceStore, NamedError, NamedSessions, parse_names
from .model_selection import parse_model_selection, public_model_catalog
from .loop import ConverseSession, drive_converse_turns, parse_start_config, voice_system_prompt
from .settings import Settings, load_settings
from .tracing import force_flush

logger = logging.getLogger(__name__)

ROUTE_PATH = "/v1/audio/converse"
VOICE_WS_PROTOCOL = "hermes-voice-v1"
# The client must send its ``start`` frame within this window.
FIRST_FRAME_TIMEOUT = 5.0
DEFAULT_LOOPBACK = "http://127.0.0.1:8642"
# Loopback HTTP: no total deadline (the SSE stream lives as long as a turn), bounded connect, and a
# read gap well past the server's 10 s SSE keepalive.
HTTP_TIMEOUT = ClientTimeout(total=None, connect=5, sock_read=90)
REPLACEMENT_TIMEOUT = 12.0


@dataclass
class _NamedConnection:
    done: asyncio.Event = field(default_factory=asyncio.Event)
    replaced: asyncio.Event = field(default_factory=asyncio.Event)
    session: Any = None
    client: Any = None


def loopback_base_url(adapter: Any, override: str = "") -> str:
    """Where this server answers on loopback: the ``api_base_url`` setting when given, else the
    adapter's listen address (a wildcard bind is reached through 127.0.0.1 / [::1])."""
    if override:
        return override.rstrip("/")
    host = str(getattr(adapter, "_host", "") or "127.0.0.1")
    port = int(getattr(adapter, "_port", 0) or 8642)
    if host in ("0.0.0.0", "", "*"):
        host = "127.0.0.1"
    elif host == "::":
        host = "[::1]"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


class ConverseRoute:
    """The route handler with its two seams: how settings are read and where the API server is.

    When cross-device arbitration is enabled, one :class:`~.arbitrate.Arbitrator` is shared by
    every connection on this route to recommend a device for each wake activation.
    """

    def __init__(self, *, settings_getter: Callable[..., Any], base_url_resolver: Callable[[], str],
                 profile_name: str = "default", hub: Optional[ConversationHub] = None,
                 device_store: Optional[DeviceStore] = None) -> None:
        self._settings_getter = settings_getter
        self._base_url = base_url_resolver
        self._profile_name = profile_name
        self.hub = hub or ConversationHub()
        self._arbitrator: Optional[Arbitrator] = None
        self._arbitrator_lock = asyncio.Lock()
        self._devices = device_store or DeviceStore()
        self._named_lock = asyncio.Lock()
        self._named_connections: dict[str, _NamedConnection] = {}

    async def _arbitrator_for_settings(self, settings: Settings) -> Optional[Arbitrator]:
        """Lazily create the shared arbitrator when arbitration is enabled."""
        if not settings.arbitrate_enabled:
            return None
        async with self._arbitrator_lock:
            if self._arbitrator is None:
                from .arbitrate import LocalBus

                self._arbitrator = Arbitrator(
                    bus=LocalBus(),
                    policy=settings.arbitrate_policy,
                    wake_window=settings.arbitrate_wake_window_seconds,
                )
                logger.info(
                    "converse: arbitration enabled (policy=%s, wake_window=%.2fs)",
                    settings.arbitrate_policy, settings.arbitrate_wake_window_seconds,
                )
            return self._arbitrator

    def wire(self, app: web.Application) -> bool:
        """Add the route; False when the API server's router is already frozen (the plugin was
        enabled while the gateway was running), in which case a restart registers it."""
        if getattr(app.router, "frozen", False):
            logger.warning("converse: the API server is already running; restart the gateway to "
                           "register %s", ROUTE_PATH)
            return False
        app.router.add_get(ROUTE_PATH, self.handle)
        app.on_cleanup.append(_flush_tracing)
        async def close_devices(app):
            await asyncio.to_thread(self._devices.close)
        app.on_cleanup.append(close_devices)
        app.router.add_post("/v1/audio/conversations/{conversation_id}/output", self.output)
        return True

    def _settings(self) -> Settings:
        return load_settings(self._settings_getter)

    def _profile_allowed(self, profile: Optional[str]) -> bool:
        # The bare route serves the profile that owns the listener; a /p/<profile>/ mirror waits on
        # plugin routes registering ahead of the API server's catch-all (upstream PR B).
        return profile is None or profile in ("default", self._profile_name)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        base_url = self._base_url()
        async with ClientSession(timeout=HTTP_TIMEOUT) as http:
            # (A) Subprotocol key: verified pre-prepare so a bad key rejects the upgrade.
            offered = offered_key_protocol(request.headers.get("Sec-WebSocket-Protocol", ""))
            if offered is not None:
                if not await verify_api_key(base_url, offered, http=http):
                    raise web.HTTPUnauthorized()
                ws = web.WebSocketResponse(heartbeat=30.0, protocols=(VOICE_WS_PROTOCOL,))
                await ws.prepare(request)
                api_key = offered
            else:
                # (B) No subprotocol: accept, then require a start frame whose start.key verifies.
                ws = web.WebSocketResponse(heartbeat=30.0)
                await ws.prepare(request)
                api_key = None

            async def _verify(candidate: str) -> bool:
                return await verify_api_key(base_url, candidate, http=http)

            frame = await _await_start_frame(ws, verify=None if api_key else _verify)
            if frame is None:
                return ws
            if api_key is None:
                api_key = str(frame.get("key") or "")
            server_recv_unix_ms = int(time.time() * 1000)
            settings = self._settings()
            try:
                names = parse_names(frame)
            except NamedError as exc:
                await _fail(ws, exc.error, exc.code)
                return ws
            if names is not None:
                return await self._named_handle(ws, frame, names, api_key, http, base_url,
                                                settings, server_recv_unix_ms)
            input_rate, output_rate, quiet_interval, name, profile = parse_start_config(
                frame, default_name=settings.name)
            if not self._profile_allowed(profile):
                await _fail(ws, "profile_unsupported", 4400)
                return ws
            requested = frame.get("conversation_id")
            if requested is not None:
                if not isinstance(requested, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", requested):
                    await _fail(ws, "bad_conversation_id", 4400)
                    return ws
                # Delegate conversation existence/profile authorization to Hermes's public API.
                async with http.get(f"{base_url}/api/sessions/{requested}",
                                    headers={"Authorization": f"Bearer {api_key}"},
                                    allow_redirects=False) as response:
                    if response.status != 200:
                        await _fail(ws, "conversation_unavailable", 4403)
                        return ws
                    existing = (await response.json()).get("session", {})
                    if existing.get("source") != "api_server":
                        await _fail(ws, "conversation_source_unsupported", 4403)
                        return ws
            try:
                synth, cap, session = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: _resolve_session(settings, input_rate, output_rate, quiet_interval))
            except Exception:
                logger.exception("converse setup failed")
                await _fail(ws, "converse setup failed", 1011)
                return ws
            if requested is not None:
                session.session_id = requested
            else:
                # Persist a real API conversation before ready; the ID can be reattached later.
                async with http.post(f"{base_url}/api/sessions",
                                     json={"source": "api_server", "id": f"voice_{uuid.uuid4().hex}"},
                                     headers={"Authorization": f"Bearer {api_key}"},
                                     allow_redirects=False) as response:
                    if response.status != 201:
                        await _fail(ws, "conversation_unavailable", 1011)
                        return ws
                    body = await response.json()
                    session.session_id = body["session"]["id"]
            try:
                attachment = self.hub.attach(session.session_id, session)
            except ValueError:
                await _fail(ws, "conversation_in_use", 4409)
                return ws
            try:
                await self._connected(ws, session, api_key, http, base_url, synth, cap, name,
                                      quiet_interval, settings, attachment, server_recv_unix_ms, input_rate, output_rate,
                                      transcripts=frame.get("transcripts") is True,
                                      models=frame.get("models") is True)
            finally:
                self.hub.detach(session.session_id, attachment)
                session.stop()
            return ws

    async def _named_handle(self, ws, frame, names, api_key, http, base_url, settings, recv_ms):
        connection = _NamedConnection()
        device = None
        attachment = None
        try:
            async with self._named_lock:
                device = await asyncio.to_thread(self._devices.reserve, *names)
                sessions = NamedSessions(base_url, api_key, http)
                old = self._named_connections.get(device.recipient_id)
                if old is not None:
                    if not old.done.is_set():
                        # Fence before closing: no further PCM/control can enter the old session.
                        if old.session is not None:
                            old.session.stop()
                        old.replaced.set()
                        try:
                            await asyncio.wait_for(old.done.wait(), REPLACEMENT_TIMEOUT)
                        except TimeoutError:
                            raise NamedError('conversation_busy', 4404) from None
                    pending = old.client.unsettled_run_ids if old.client is not None else set()
                    if old.client is not None and old.client.admission_uncertain:
                        raise NamedError('conversation_busy', 4404)
                    if not await sessions.settled(pending):
                        raise NamedError('conversation_busy', 4404)
                conversation_id, live_id, history = await sessions.resolve(device, self._devices)
                input_rate, output_rate, quiet_interval, _, profile = parse_start_config(frame)
                if not self._profile_allowed(profile):
                    raise NamedError('profile_unsupported', 4400)
                synth, cap, session = await asyncio.to_thread(
                    _resolve_session, settings, input_rate, output_rate, quiet_interval)
                connection.session = session
                attachment = self.hub.attach(conversation_id, session)
                self.hub.alias(live_id, attachment)
                self._named_connections[device.recipient_id] = connection
            await self._connected(ws, session, api_key, http, base_url, synth, cap, names[0],
                                  quiet_interval, settings, attachment, recv_ms, input_rate, output_rate,
                                  named=(device, connection, history), transcripts=frame.get("transcripts") is True,
                                  models=frame.get("models") is True)
        except NamedError as exc:
            await _fail(ws, exc.error, exc.code)
        except (ClientError, TimeoutError, ValueError):
            await _fail(ws, 'conversation_unavailable', 4404)
        except asyncio.CancelledError:
            # Replacement owns the close code; cancellation still runs stop/settlement cleanup.
            pass
        except Exception:
            logger.exception('named converse setup failed')
            await _fail(ws, 'conversation_unavailable', 4404)
        finally:
            if attachment is not None:
                self.hub.detach(attachment.conversation_id, attachment)
            if connection.session is not None:
                connection.session.stop()
            connection.done.set()
            if (device is not None and self._named_connections.get(device.recipient_id) is connection
                    and (connection.client is None or (not connection.client.unsettled_run_ids
                                                      and not connection.client.admission_uncertain))):
                self._named_connections.pop(device.recipient_id, None)
        return ws

    async def _connected(self, ws, session, api_key, http, base_url, synth, cap, name,
                         quiet_interval, settings, attachment, server_recv_unix_ms, input_rate, output_rate,
                         named=None, transcripts=False, models=False):
        client = RunsClient(base_url, api_key, http=http, approval_choice=settings.approval_choice)
        ready = {
            "type": "ready",
            "transcripts": transcripts,
            "input": {"sample_rate": input_rate, "format": "pcm16", "block_ms": 30},
            "output": {"sample_rate": output_rate, "format": "pcm16"},
            "session_id": session.session_id,
            "conversation_id": session.session_id,
            "server_recv_unix_ms": server_recv_unix_ms,
            "server_send_unix_ms": int(time.time() * 1000),
        }
        if models:
            try:
                ready["model_catalog"] = public_model_catalog(await client.model_options())
            except Exception:
                logger.warning("converse model catalog unavailable", exc_info=True)
                ready["model_catalog"] = {"providers": []}
        if named is not None:
            device, connection, history = named
            ready.pop('conversation_id')
            ready.update(recipient_id=device.recipient_id, assistant_name=device.assistant_name,
                         device_name=device.device_name)
        ready["server_send_unix_ms"] = int(time.time() * 1000)
        await ws.send_json(ready)
        arbitrator = await self._arbitrator_for_settings(settings)
        # A configured label is a prefix: each socket needs its own candidate identity.
        device_id = f"{settings.arbitrate_device_id or 'device'}_{uuid.uuid4().hex}"
        if arbitrator is not None:
            await arbitrator.register(device_id)
        # The only place a socket's device identity meets its session id; turn and onset lines
        # carry the session id, so this is what attributes them to a device.
        logger.info("converse session: session=%s device_id=%s name=%r arbitrated=%s",
                    getattr(session, "session_id", None), device_id, name, arbitrator is not None)
        if named is not None:
            connection.client = client
        await _serve(ws, session=session, synth=synth, cap=cap, client=client, settings=settings,
                     name=name, quiet_interval=quiet_interval, arbitrator=arbitrator,
                     device_id=device_id, hub=self.hub, attachment=attachment,
                     named_device=named[0] if named else None, history=history if named else None,
                     named_connection=named[1] if named else None, transcripts=transcripts)

    async def output(self, request: web.Request) -> web.Response:
        header = request.headers.get("Authorization", "")
        key = header[7:] if header.startswith("Bearer ") else ""
        async with ClientSession(timeout=HTTP_TIMEOUT) as http:
            if not await verify_api_key(self._base_url(), key, http=http):
                raise web.HTTPUnauthorized()
        try:
            body = await request.json()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(reason="bad_output")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(reason="bad_output")
        text, event_id = body.get("text"), body.get("event_id")
        expires = body.get("expires_at_unix_ms")
        action = body.get("next_action", "standby")
        if (not isinstance(text, str) or not text.strip() or len(text) > 20000
                or not isinstance(event_id, str) or not 1 <= len(event_id) <= 128
                or isinstance(expires, bool) or not isinstance(expires, (int, float))
                or not 0 <= expires <= 253402300799999
                or not math.isfinite(expires) or action not in ("listen", "standby")):
            raise web.HTTPBadRequest(reason="bad_output")
        result = self.hub.publish(request.match_info["conversation_id"], text, event_id,
                                  expires_at=expires / 1000, next_action=action)
        status = {"accepted": 202, "duplicate": 200, "expired": 410,
                  "not_attached": 409, "busy": 429}[result]
        return web.json_response({"status": result, "event_id": event_id}, status=status)


def _resolve_session(settings: Settings, input_rate: int, output_rate: int, quiet_interval: float):
    """Blocking provider/config resolution — runs off the event loop. Returns ``(synth, cap,
    session)``: a synthesizer emitting int16 PCM at *output_rate* (NEVER ``None``), its per-request
    text cap, and a fresh :class:`ConverseSession` capturing at *input_rate*."""
    import numpy as np

    from . import speech
    from .synth import resample_synth, resolve_synthesizer

    stt_model = speech.stt_model_override()
    tts_config = speech.load_tts_config()
    synth = resample_synth(resolve_synthesizer(tts_config), output_rate)
    cap = speech.tts_text_cap(tts_config)

    def _transcribe(wav_path: str) -> str:
        return speech.transcribe_wav(wav_path, model=stt_model)

    session = ConverseSession(
        np, input_rate=input_rate, quiet_interval=quiet_interval, transcribe=_transcribe,
        endpoint_model=settings.endpoint_model, endpoint_threshold=settings.endpoint_threshold,
        vad_model=settings.vad_model, capture_no_data_seconds=settings.capture_no_data_seconds)
    return synth, cap, session


def _wake_number(raw: Any, *, maximum: Optional[float] = None) -> Optional[float]:
    # Missing or unusable optional signals lose their ranking tier.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        number = float(raw)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0 or (maximum is not None and number > maximum):
        return None
    return number


async def _wake_response(frame: dict, *, arbitrator: Optional[Arbitrator], device_id: str) -> dict:
    """Answer a cooperative claim without changing any audio/turn state."""
    claim_id, wake_word = frame.get("claim_id"), frame.get("wake_word")
    if (not isinstance(claim_id, str) or not claim_id.strip()
            or not isinstance(wake_word, str) or not wake_word.strip()):
        return {"type": "error", "error": "bad_wake"}
    granted, winner = True, None
    if arbitrator is not None:
        granted, winner, _ = await arbitrator.claim_wake(
            device_id, claim_id=claim_id, wake_word=wake_word,
            confidence=_wake_number(frame.get("confidence"), maximum=1),
            heard_at=_wake_number(frame.get("heard_at")),
            volume=_wake_number(frame.get("volume"), maximum=1))
    reply = {"type": "wake_grant" if granted else "wake_deny",
             "claim_id": claim_id, "wake_word": wake_word}
    if not granted and winner is not None:
        reply["winner_device_id"] = winner
    return reply


async def _fail(ws: web.WebSocketResponse, error: str, code: int) -> None:
    with contextlib.suppress(Exception):
        await ws.send_json({"type": "error", "error": error})
        await ws.close(code=code)


async def _await_start_frame(ws: web.WebSocketResponse, *,
                             verify: Optional[Callable[[str], Awaitable[bool]]]) -> Optional[dict]:
    """Require ``{"type":"start", ...}`` as the client's FIRST frame within the deadline. With
    *verify* given (no subprotocol key), ``start.key`` must pass it. On failure sends an error
    frame, closes (4400 bad_start / 4401 unauthorized) and returns ``None``."""
    try:
        msg = await asyncio.wait_for(ws.receive(), timeout=FIRST_FRAME_TIMEOUT)
    except TimeoutError:
        await _fail(ws, "bad_start", 4400)
        return None
    if msg.type != web.WSMsgType.TEXT:
        await _fail(ws, "bad_start", 4400)
        return None
    try:
        frame = json.loads(msg.data)
    except (ValueError, TypeError):
        await _fail(ws, "bad_start", 4400)
        return None
    if not isinstance(frame, dict) or frame.get("type") != "start":
        await _fail(ws, "bad_start", 4400)
        return None
    if verify is not None and not await verify(str(frame.get("key") or "")):
        await _fail(ws, "unauthorized", 4401)
        return None
    return frame


async def _serve(ws: web.WebSocketResponse, *, session: ConverseSession, synth: Any, cap: int,
                 client: RunsClient, settings: Settings, name: str, quiet_interval: float,
                 arbitrator: Optional[Arbitrator] = None,
                 device_id: str = "", hub=None, attachment=None, named_device=None,
                 history: Optional[list[dict[str, str]]] = None, named_connection=None,
                 transcripts: bool = False) -> None:
    """Run the client pump and the turn driver until the socket ends; always stop the live run."""
    loop = asyncio.get_running_loop()
    session.enable_turn_control()
    session.start()
    # The Runs API continues this conversation across turns by session id; one per socket.
    session_id = attachment.conversation_id if attachment is not None else session.session_id
    history = [] if history is None else history
    allow_signoff = quiet_interval > 0
    default_model = (settings.model, settings.provider)
    selected_model = default_model
    wake_tasks: set[asyncio.Task] = set()
    async def _reply_wake(frame: dict) -> None:
        try:
            reply = await _wake_response(frame, arbitrator=arbitrator, device_id=device_id)
            await ws.send_json(reply)
        except Exception:
            logger.debug("converse wake reply failed", exc_info=True)

    async def _pump_client() -> None:
        nonlocal selected_model
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    session.stream.feed(msg.data)
                elif msg.type == web.WSMsgType.TEXT:
                    try:
                        frame = json.loads(msg.data)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(frame, dict):
                        continue
                    if frame.get("stop"):
                        break
                    if frame.get("commit"):
                        session.commit()
                    if frame.get("type") == "drained":
                        session.notify_drained(frame.get("turn_id"))
                    if frame.get("type") == "cancel":
                        session.cancel(frame.get("turn_id"))
                    if frame.get("type") == "set_model":
                        request_id = frame.get("request_id")
                        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id):
                            await ws.send_json({"type": "model_error", "error": "bad_request_id"})
                            continue
                        try:
                            choice = parse_model_selection(frame)
                        except ValueError:
                            await ws.send_json({"type": "model_error", "request_id": request_id,
                                                "error": "bad_model_selection"})
                            continue
                        selected_model = default_model if choice == (None, None) else choice
                        await ws.send_json({"type": "model_changed", "request_id": request_id,
                                            "model": selected_model[0], "provider": selected_model[1],
                                            "uses_default": choice == (None, None), "applies_to": "next_turn"})
                    if frame.get("type") == "wake":
                        # Do not block PCM, cancel, stop, or a new claim while an election runs.
                        task = asyncio.create_task(_reply_wake(frame))
                        wake_tasks.add(task)
                        task.add_done_callback(wake_tasks.discard)
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                    break
        except Exception:
            logger.debug("converse client pump ended", exc_info=True)
        session.stop()

    async def _run_turn(transcript: str, on_delta, *, interrupted: bool) -> tuple[str, Optional[str]]:
        model, provider = selected_model  # Snapshot once; do not alter a run already in flight.
        with hub.owned_run(attachment, transcript) if hub is not None else contextlib.nullcontext() as owned:
            result = await client.run_turn(
                text=transcript, session_id=session_id, on_delta=on_delta,
                instructions=voice_system_prompt(
                    name, allow_signoff=allow_signoff, end_phrases=settings.end_phrases,
                    device_name=named_device.device_name if named_device else None,
                    recipient_id=named_device.recipient_id if named_device else None),
                model=model, provider=provider,
                idempotency_key=f"converse-{session_id}-{uuid.uuid4().hex}")
            if owned is not None and result.error is None:
                owned.reply = result.reply
        return result.reply, result.error

    def _interrupt_turn() -> None:
        # Barge-in: stop the in-flight run so the barge utterance isn't queued behind it.
        loop.create_task(client.stop())

    async def _drive_turns() -> None:
        await drive_converse_turns(
            session=session, synth=synth, cap=cap, loop=loop,
            send_json=ws.send_json, send_bytes=ws.send_bytes, run_turn=_run_turn, history=history,
            quiet_interval=quiet_interval, interrupt_turn=_interrupt_turn, transcripts=transcripts,
            tool_calls=lambda: client.tool_calls,
            turn_timeout=settings.turn_timeout, tail_fallback=settings.tail_fallback,
            max_tts_chars=settings.max_tts_chars,
            end_phrases=settings.end_phrases,
        )

    pump = asyncio.ensure_future(_pump_client())
    driver = asyncio.ensure_future(_drive_turns())
    fence = asyncio.create_task(named_connection.replaced.wait()) if named_connection else None
    try:
        done, _ = await asyncio.wait({pump, driver, *([fence] if fence else [])},
                                     return_when=asyncio.FIRST_COMPLETED)
        if driver in done:
            driver.result()  # surface a crash
        else:
            # The client went away mid-turn: abandon the turn now (its run is stopped below) rather
            # than wait for a reply nobody will hear.
            driver.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await driver
    except (asyncio.CancelledError, RuntimeError, ConnectionResetError):
        pass
    except Exception:
        logger.exception("converse loop crashed")
    finally:
        session.stop()
        pump.cancel()
        driver.cancel()
        for task in wake_tasks:
            task.cancel()
        if fence is not None:
            fence.cancel()
        await asyncio.gather(pump, driver, *wake_tasks, *([fence] if fence else []), return_exceptions=True)
        if arbitrator is not None:
            with contextlib.suppress(Exception):
                await arbitrator.unregister(device_id)
        with contextlib.suppress(Exception):
            await client.stop()  # a socket that drops mid-turn must not orphan the run
        with contextlib.suppress(Exception):
            if named_connection is not None and named_connection.replaced.is_set():
                # Send the close frame, but a peer that is not reading cannot hold up admission.
                await asyncio.wait_for(ws.close(code=4409, message=b'attachment_replaced'), timeout=1.0)
            else:
                await ws.close()


async def _flush_tracing(app: web.Application) -> None:
    # Batch exporters may block; keep their bounded shutdown drain off the event loop.
    await asyncio.to_thread(force_flush)


def make_platform_factory(ctx: Any, *, hub: Optional[ConversationHub] = None) -> Callable[[Any, Any], None]:
    """The ``register_platform_handler("api_server", ...)`` factory: ``factory(app, adapter)``."""

    def factory(app: web.Application, adapter: Any) -> None:
        def _base_url() -> str:
            return loopback_base_url(adapter, str(ctx.get_config("api_base_url", "") or ""))

        route = ConverseRoute(settings_getter=ctx.get_config, base_url_resolver=_base_url,
                              profile_name=getattr(ctx, "profile_name", "default"), hub=hub)
        if route.wire(app):
            logger.info("converse: %s registered on the API server", ROUTE_PATH)

    return factory
