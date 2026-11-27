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
  client → ``{"type":"start", "key":?, "assistant_name":"...", "device_name":"...",
             "input_rate":?, "output_rate":?, "quiet_interval":?}`` — both names are REQUIRED and
           a ``profile``/``name``/``conversation``/``conversation_id`` field closes 4400
           ``bad_start`` — then binary PCM16 mono frames at ``input_rate`` (30 ms blocks preferred),
           ``{"stop": true}`` to end, ``{"commit": true}`` to force an endpoint,
           ``{"type":"cancel","turn_id":?}`` to cancel the current turn and keep the session,
           ``{"type":"heard","wake_word":"..."}`` a wake word heard during a turn (logged only),
           ``{"type":"state","turn_id":?,"state":...}`` the state the device now shows (leaving
           ``speaking`` means the reply's audio has played or been discarded), ``{"type":"countdown",
           "countdown_id":...,"ringing":true}`` or ``..."ended":true}`` a countdown ringing or ended,
           ``{"type":"wake","claim_id":"...","wake_word":"...", ...}`` requests an
           advisory wake recommendation; see ``specs/client.md``. Accepted interruptions are captured as continuations.
           ``{"type":"wake_word_set","phrase":"...","status":"ok"}`` the result of a wake-word switch.
           ``{"type":"controls","muted":...,"volume":...}`` the device's mute and volume.
  server → ``ready`` (rates, ``session_id``, ``wake_word``, clock-skew timestamps), ``set_wake_word``,
           ``controls`` (set the device's mute and volume),
           ``transcript``, ``thinking``,
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

from . import _server_recording as diagnostic
from . import command_catalog
from .admin import ROUTE_PATH as ADMIN_ROUTE_PATH
from .admin import Admin, client_address
from .approvals import phrase, summary

from .arbitrate import Arbitrator
from .audio_transport import AudioTransport, audio_format
from .opus import OpusError
from .auth import offered_key_protocol, verify_api_key
from .catchup import Detached, MessageIds, Relay, replay_frames
from .client_diagnostics import ClientDiagnostics
from .conversation import ConversationHub
from .named import DeviceStore, NamedError, parse_names
from .platform_adapter import GatewayTurns
from .model_selection import public_model_catalog
from .instructions import load_voice_guidance, voice_system_prompt
from .loop import (ConversationOutput, ConverseSession, drive_converse_turns,
                   parse_start_config)
from .settings import Settings, load_settings
from .tracing import ConnectionTrace, emit_span, force_flush
from .wake_words import ENGINES as WAKE_WORD_FORMATS
from .wake_words import ROUTE_PREFIX as WAKE_ROUTE_PREFIX
from .wake_words import WakeWordLink, WakeWords, device_wake_words

logger = logging.getLogger(__name__)

ROUTE_PATH = "/v1/audio/converse"
VOICE_WS_PROTOCOL = "hermes-voice-v1"
# The client must send its ``start`` frame within this window.
FIRST_FRAME_TIMEOUT = 5.0
DEFAULT_LOOPBACK = "http://127.0.0.1:8642"
# Loopback HTTP to the API server: key verification, the model catalog and session lookups.
HTTP_TIMEOUT = ClientTimeout(total=30, connect=5)
REPLACEMENT_TIMEOUT = 12.0
_CONNECTION_TRACE = web.ResponseKey("voice.connection", ConnectionTrace)  # the socket's connection span
GATEWAY_WAIT = 30.0


@dataclass
class _NamedConnection:
    done: asyncio.Event = field(default_factory=asyncio.Event)
    replaced: asyncio.Event = field(default_factory=asyncio.Event)
    session: Any = None


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
                 hub: Optional[ConversationHub] = None,
                 device_store: Optional[DeviceStore] = None,
                 settings_setter: Optional[Callable[[str, Any], None]] = None,
                 wake_words: Optional[WakeWords] = None) -> None:
        self._settings_getter = settings_getter
        self._settings_setter = settings_setter
        self._wake_words = wake_words or WakeWords()
        self._wake_setting_lock = asyncio.Lock()
        self._base_url = base_url_resolver
        self.hub = hub or ConversationHub()
        self._arbitrator: Optional[Arbitrator] = None
        self._arbitrator_lock = asyncio.Lock()
        self._devices = device_store or DeviceStore()
        self._named_locks: dict[tuple[str, str], asyncio.Lock] = {}  # per device: one device's setup never waits on another's
        self._named_connections: dict[str, _NamedConnection] = {}
        self.admin = Admin(self._devices, self.hub, base_url_resolver)

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
                    volume_offsets_db=settings.arbitrate_volume_offsets_db,
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
        app.router.add_get(ADMIN_ROUTE_PATH, self.admin.handle)
        app.router.add_get(WAKE_ROUTE_PREFIX + "/{slug}/{file}", self.wake_file)
        app.cleanup_ctx.append(diagnostic.maintenance)
        app.on_startup.append(self.admin.run_purge)
        app.on_cleanup.append(self.admin.close)
        app.on_cleanup.append(_flush_tracing)
        async def close_devices(app):
            await asyncio.to_thread(self._devices.close)
        app.on_cleanup.append(close_devices)
        return True

    async def _gateway(self) -> Any:
        """The connected converse platform adapter. At startup a device can dial in before the
        platform has connected, so wait for it; None when this Hermes is not hosting it."""
        deadline = time.monotonic() + GATEWAY_WAIT
        while self.hub.gateway is None and self.hub.gateway_expected and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        return self.hub.gateway

    def _settings(self) -> Settings:
        return load_settings(self._settings_getter)

    async def wake_file(self, request: web.Request) -> web.StreamResponse:
        """``GET /v1/audio/wake/<slug>/<file>``: a cached wake-word model file, for the voice API key."""
        scheme, _, key = request.headers.get("Authorization", "").partition(" ")
        async with ClientSession(timeout=HTTP_TIMEOUT) as http:
            if scheme.lower() != "bearer" or not await verify_api_key(self._base_url(), key.strip(), http=http):
                raise web.HTTPUnauthorized()
        path = await asyncio.to_thread(self._wake_words.file, request.match_info["slug"],
                                       request.match_info["file"])
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def _set_device_wake_word(self, device_name: str, phrase: str) -> None:
        """Write one device's entry in ``device_wake_words``."""
        if self._settings_setter is None:
            raise RuntimeError("settings are read-only")
        async with self._wake_setting_lock:
            words = {**device_wake_words(self._settings_getter), device_name: phrase}
            await asyncio.to_thread(self._settings_setter, "device_wake_words", words)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        """One ``voice.connection`` span per socket, from accepting it to closing it."""
        conn = ConnectionTrace()
        try:
            return await self._handle(request, conn)
        except web.HTTPException as exc:
            conn.mark("server", exc.status)  # refused before the upgrade
            raise
        except BaseException:
            conn.mark("lost")
            raise
        finally:
            conn.end()

    async def _handle(self, request: web.Request, conn: ConnectionTrace) -> web.StreamResponse:
        base_url = self._base_url()
        async with ClientSession(timeout=HTTP_TIMEOUT) as http:
            # (A) Subprotocol key: verified pre-prepare so a bad key rejects the upgrade.
            offered = offered_key_protocol(request.headers.get("Sec-WebSocket-Protocol", ""))
            if offered is not None:
                if not await verify_api_key(base_url, offered, http=http):
                    raise web.HTTPUnauthorized()
                ws = web.WebSocketResponse(heartbeat=30.0, protocols=(VOICE_WS_PROTOCOL,))
                await ws.prepare(request)
                _attach_trace(ws, conn)
                api_key = offered
            else:
                # (B) No subprotocol: accept, then require a start frame whose start.key verifies.
                ws = web.WebSocketResponse(heartbeat=30.0)
                await ws.prepare(request)
                _attach_trace(ws, conn)
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
                audio_format(frame)
            except NamedError as exc:
                await _fail(ws, exc.error, exc.code)
                return ws
            except ValueError:
                await _fail(ws, "bad_start", 4400)
                return ws
            conn.set(device_name=names[1])
            return await self._named_handle(ws, frame, names, api_key, http, base_url,
                                            settings, server_recv_unix_ms, client_address(request))

    async def _named_handle(self, ws, frame, names, api_key, http, base_url, settings, recv_ms, address=None):
        connection = _NamedConnection()
        device = None
        attachment = None
        audio = None
        try:
            lock = self._named_locks.setdefault(tuple(names), asyncio.Lock())
            async with lock:
                device = await asyncio.to_thread(self._devices.reserve, *names)
                old = self._named_connections.get(device.recipient_id)
                if old is not None:
                    if not old.done.is_set():
                        # Fence before closing: no further PCM/control can enter the old session.
                        old.replaced.set()
                        try:
                            await asyncio.wait_for(old.done.wait(), REPLACEMENT_TIMEOUT)
                        except TimeoutError:
                            raise NamedError('conversation_busy', 4404) from None
                gateway = await self._gateway()
                if gateway is None:
                    raise NamedError()
                conversation_id = device.candidate_id.removeprefix('voice_')
                source = gateway.source_for(chat_id=conversation_id, title=device.title,
                                            recipient_id=device.recipient_id, device_name=device.device_name)
                turns = GatewayTurns(gateway, source, await gateway.bind(source))
                input_rate, output_rate, quiet_interval = parse_start_config(frame)
                try:
                    audio = AudioTransport(audio_format(frame), input_rate, output_rate)
                except OSError:
                    # A valid format this server cannot set up (Opus without libopus).
                    logger.exception('converse: cannot set up %s audio', audio_format(frame))
                    raise NamedError('audio_unavailable', 4503) from None
                synth, cap, session = await asyncio.to_thread(
                    _resolve_session, settings, input_rate, output_rate, quiet_interval)
                connection.session = session
                attachment = self.hub.attach(conversation_id, session, device.assistant_name)
                self._named_connections[device.recipient_id] = connection
            await self._connected(ws, session, api_key, http, base_url, synth, cap, names[0],
                                  quiet_interval, settings, attachment, recv_ms, input_rate, output_rate,
                                  named=(device, connection), transcripts=frame.get("transcripts") is True,
                                  models=frame.get("models") is True, turns=turns,
                                  wake_word_format=frame.get("wake_word_format"),
                                  after=frame.get("after"), audio=audio, address=address)
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
            if audio is not None:
                audio.close()
            if attachment is not None:
                self.hub.detach(attachment.conversation_id, attachment)
                self.admin.detach(device.device_name, attachment)
            if connection.session is not None:
                connection.session.stop()
            connection.done.set()
            if device is not None and self._named_connections.get(device.recipient_id) is connection:
                self._named_connections.pop(device.recipient_id, None)
        return ws

    async def _connected(self, ws, session, api_key, http, base_url, synth, cap, name,
                         quiet_interval, settings, attachment, server_recv_unix_ms, input_rate, output_rate,
                         *, named, transcripts=False, models=False, turns=None, after=None, audio=None,
                         wake_word_format=None, address=None):
        client = turns
        device, connection = named
        adopted = self.hub.detached.pop(attachment.conversation_id, None)
        if adopted is not None and adopted.driver.done():
            adopted.session.stop()
            adopted = None
        if adopted is not None:
            session.stop()
            session = adopted.session
            adopted.control["detached"] = False
            session.set_quiet_interval(quiet_interval)
            connection.session = attachment.session = session
            client = adopted.client
        # Only a device that names the model format it runs has a server-managed wake word.
        wake_word = WakeWordLink(ws, self._wake_words, device.device_name, wake_word_format,
                                 get=self._settings_getter, persist=self._set_device_wake_word
                                 ) if wake_word_format in WAKE_WORD_FORMATS else None
        offered = await wake_word.ready() if wake_word is not None else None
        ready = {
            "type": "ready",
            "transcripts": transcripts,
            "input": {"sample_rate": input_rate, "format": "pcm16", "block_ms": 30},
            "output": {"sample_rate": output_rate, "format": "pcm16"},
            "session_id": session.session_id,
            "recipient_id": device.recipient_id,
            "assistant_name": device.assistant_name,
            "device_name": device.device_name,
            "server_recv_unix_ms": server_recv_unix_ms,
            "server_send_unix_ms": int(time.time() * 1000),
        }
        if offered is not None:
            ready["wake_word"] = offered
        if audio is not None and audio.encoder is not None:
            ready["input"].update(format="opus", block_ms=20)
            ready["output"].update(format="opus", block_ms=20)
        catalog = {}
        if models:
            try:
                catalog = public_model_catalog(await _model_options(base_url, api_key, http))
            except Exception:
                logger.warning("converse model catalog unavailable", exc_info=True)
            ready["model_catalog"] = catalog or {"providers": []}
        default_route = (catalog.get("model"), catalog.get("provider"))
        await client.apply_voice_default(settings.model, settings.provider)
        ready["current_model"] = await asyncio.to_thread(client.current_model, default_route)
        history = getattr(client, "history", None)
        rows = None
        if transcripts and history is not None:
            rows = await asyncio.to_thread(history.rows)
            if isinstance(after, str):
                # True when the client's cursor is not a message of the device's current session.
                ready["new_session"] = not (after.isdigit() and int(after) in {row["id"] for row in rows or []})
        guidance = await asyncio.to_thread(load_voice_guidance)
        ready["server_send_unix_ms"] = int(time.time() * 1000)
        await ws.send_json(ready)
        # Catch the client up before anything live, then hand it any turn still running.
        relay = adopted.relay if adopted is not None else Relay(ws, audio=audio, source_rate=output_rate)
        running = adopted.client.message_key if adopted is not None and not adopted.client.settled else None
        if rows is not None:
            for replay in replay_frames(rows or [], after if isinstance(after, str) else None):
                # The running turn's reply so far comes from the turn itself, below.
                if not (running and replay["role"] == "assistant" and replay.get("turn_id") == running):
                    await ws.send_json(replay)
        if adopted is not None:
            client = adopted.client
        device_name = device.device_name
        session.device_name = device_name
        if hasattr(session, "attach_device"):
            session.attach_device(device_name)
        connection_trace = _connection_trace(ws)
        if connection_trace is not None:
            connection_trace.set(**{"session.id": session.session_id, "device_name": device_name})
        arbitrator = await self._arbitrator_for_settings(settings)
        # A configured label is a prefix: each socket needs its own candidate identity.
        device_id = f"{settings.arbitrate_device_id or 'device'}_{uuid.uuid4().hex}"
        if arbitrator is not None:
            await arbitrator.register(device_id, device_name=device_name, session_id=session.session_id)
        # The only place a socket's device identity meets its session id; turn and onset lines
        # carry the session id, so this is what attributes them to a device.
        logger.info("converse session: session=%s device_id=%s name=%r arbitrated=%s",
                    getattr(session, "session_id", None), device_id, name, arbitrator is not None)
        self.admin.attach(device_name, attachment, ws, address)
        await _serve(ws, session=session, synth=synth, cap=cap, client=client, settings=settings,
                     report=lambda frame: self.admin.reported(device_name, attachment, frame),
                     name=name, guidance=guidance, quiet_interval=quiet_interval, arbitrator=arbitrator,
                     device_id=device_id, hub=self.hub, attachment=attachment,
                     named_device=device, named_connection=connection, transcripts=transcripts,
                     relay=relay, adopted=adopted, audio=audio, wake_word=wake_word,
                     default_route=default_route, ready_frame=ready)


async def _model_options(base_url: str, api_key: str, http: ClientSession) -> dict[str, Any]:
    """The API server's authenticated model inventory, for the opt-in catalog in ``ready``."""
    params = {"include_unconfigured": "false", "refresh": "false"}
    async with asyncio.timeout(8):
        async with http.get(f"{base_url}/api/model/options", params=params, allow_redirects=False,
                            headers={"Authorization": f"Bearer {api_key}"}) as response:
            result = await response.json() if response.status == 200 else None
    if not isinstance(result, dict) or not isinstance(result.get("providers"), list):
        raise ValueError("model_catalog_unavailable")
    return result


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
        smart_turn_enabled=settings.smart_turn_enabled, endpoint_threshold=settings.endpoint_threshold,
        endpoint_silence_ms=settings.endpoint_silence_ms, endpoint_max_silence_ms=settings.endpoint_max_silence_ms,
        vad_model=settings.vad_model, capture_no_data_seconds=settings.capture_no_data_seconds)
    return synth, cap, session


MAX_SAY_CHARS = 400


def _say_text(raw: Any) -> str:
    """The text of a client ``say`` frame: a nonempty string, trimmed and capped."""
    return raw.strip()[:MAX_SAY_CHARS] if isinstance(raw, str) else ""


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


async def _wake_response(frame: dict, *, arbitrator: Optional[Arbitrator], device_id: str,
                         track_sent: bool = False) -> dict:
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
            volume=_wake_number(frame.get("volume"), maximum=1), track_sent=track_sent)
    reply = {"type": "wake_grant" if granted else "wake_deny",
             "claim_id": claim_id, "wake_word": wake_word}
    if not granted and winner is not None:
        reply["winner_device_id"] = winner
    return reply


def _attach_trace(ws: web.WebSocketResponse, conn: ConnectionTrace) -> None:
    conn.ws = ws
    ws[_CONNECTION_TRACE] = conn


def _connection_trace(ws: Any) -> Optional[ConnectionTrace]:
    try:
        return ws.get(_CONNECTION_TRACE)
    except Exception:
        return None


def _mark_closed(ws: Any, by: str, code: Optional[int] = None) -> None:
    conn = _connection_trace(ws)
    if conn is not None:
        conn.mark(by, code)


def _trace_immediate_wake(frame: dict, *, session: Any, sent: bool) -> None:
    """With arbitration off a claim is granted at once: an election of one claim, marked immediate."""
    now = time.time_ns()
    claim = {"device_name": getattr(session, "device_name", None),
             "session.id": getattr(session, "session_id", None), "claim_id": frame.get("claim_id"),
             "wake_word": frame.get("wake_word"), "wake.confidence": _wake_number(frame.get("confidence"), maximum=1),
             "wake.volume": _wake_number(frame.get("volume"), maximum=1),
             "wake.heard_at": _wake_number(frame.get("heard_at")), "claim.arrival": 1,
             "claim.outcome": "granted", "decision.sent": sent}
    emit_span("voice.wake.election", now, now, {
        "election_id": uuid.uuid4().hex, "election.policy": "off", "election.window_ms": 0,
        "election.claims": 1, "election.immediate": True, "election.winner_claim_id": frame.get("claim_id"),
    }, events=[("wake.claim", now, claim)])


async def _fail(ws: web.WebSocketResponse, error: str, code: int) -> None:
    _mark_closed(ws, "server", code)
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
    if msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSING, web.WSMsgType.CLOSED):
        _mark_closed(ws, "lost", 1006) if ws.close_code in (None, 1006) else _mark_closed(ws, "client", ws.close_code)
    elif msg.type == web.WSMsgType.ERROR:
        _mark_closed(ws, "lost", 1006)
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
                 client: Any, settings: Settings, name: str, guidance: str, quiet_interval: float,
                 named_device, named_connection, arbitrator: Optional[Arbitrator] = None,
                 device_id: str = "", hub=None, attachment=None,
                 transcripts: bool = False, relay: Optional[Relay] = None,
                 adopted: Optional[Detached] = None, audio=None,
                 wake_word: Optional[WakeWordLink] = None, default_route=(None, None),
                 report: Callable[[dict], None] = lambda frame: None, ready_frame=None) -> None:
    """Run socket input and its single driver; preserve an unfinished turn on disconnect."""
    loop = asyncio.get_running_loop()
    async def _send_json(frame):
        recording.frame(frame)
        await ws.send_json(frame)

    session.enable_turn_control()
    if adopted is None:
        session.start()
    history: list[dict[str, str]] = []
    diagnostics = ClientDiagnostics(ws)
    if attachment is not None:
        # Tools run on worker threads; frames go out through the socket's own loop.
        attachment.send = lambda frame: asyncio.run_coroutine_threadsafe(_send_json(frame), loop)
        attachment.command = lambda command, args, timeout: asyncio.run_coroutine_threadsafe(
            _command_tool(command, args, timeout), loop)
        attachment.diagnostics = lambda: asyncio.run_coroutine_threadsafe(diagnostics.request(), loop)
        if wake_word is not None:
            attachment.wake_word = lambda phrase: asyncio.run_coroutine_threadsafe(
                wake_word.listing() if phrase is None else wake_word.switch(phrase), loop)
        if getattr(hub, "countdowns", None) is not None:
            hub.countdowns.attached(attachment, getattr(client, "_source", None),
                                    settings.countdown_ring_timeout_seconds, loop)
    if attachment is not None and getattr(hub, "gateway", None) is not None:
        hub.gateway.approvals.attached(attachment)  # a request still pending asks again
    model_change = None
    dispatched = False
    pending_state = None
    active_submissions = set()

    async def snapshot():
        if pending_state is not None:
            return {**pending_state, "pending": True}
        state = await asyncio.to_thread(client.current_model, default_route)
        return {**state, "pending": bool(model_change and not model_change.done())}

    async def wait_then_change(frame, earlier, approved):
        nonlocal dispatched, pending_state
        try:
            pending_state = await asyncio.to_thread(client.current_model, default_route)
            for done in earlier:
                await asyncio.shield(done)
            await client.wait_for_voice_turn()
            if not ws.closed and not named_connection.replaced.is_set():
                await _command(frame, approved=approved)
        except Exception:
            logger.warning("converse: model command failed", exc_info=True)
            if not ws.closed:
                await _send_json({"type": "command", "name": frame["name"], "args": frame["args"], "error": "failed"})
        finally:
            dispatched = False
            pending_state = None

    def queue_model_change(frame, *, approved=False):
        nonlocal model_change
        if model_change and not model_change.done():
            return False
        model_change = asyncio.create_task(wait_then_change(frame, tuple(active_submissions), approved))
        wake_tasks.add(model_change)
        model_change.add_done_callback(wake_tasks.discard)
        return True

    control = adopted.control if adopted is not None else {}
    wake_tasks: set[asyncio.Task] = set()
    if wake_word is not None:
        wake_tasks.add(asyncio.create_task(wake_word.watch()))
    relay = relay or Relay(ws)
    if adopted is not None:
        relay.attach(ws, audio=audio)
    recording = diagnostic.ServerRecording(session, output_rate=relay.source_rate or 24000)
    relay.recording = recording
    if ready_frame is not None:
        recording.frame(ready_frame)
    explicit_stop = False
    after_reply = None  # only the final reply returned by _run_turn, never an approval output
    async def _reply_wake(frame: dict) -> None:
        reply, sent = None, False
        try:
            reply = await _wake_response(frame, arbitrator=arbitrator, device_id=device_id, track_sent=True)
            if reply["type"] == "wake_grant":
                session.note_wake(reply["claim_id"])
            await _send_json(reply)
            sent = True
        except Exception:
            logger.debug("converse wake reply failed", exc_info=True)
        finally:
            claim_id = frame.get("claim_id")
            if arbitrator is not None and isinstance(claim_id, str):
                arbitrator.note_decision_sent(device_id, claim_id, sent)
            elif arbitrator is None and reply is not None and reply["type"] == "wake_grant":
                _trace_immediate_wake(frame, session=session, sent=sent)

    async def _pump_client() -> None:
        nonlocal explicit_stop
        try:
            async for msg in ws:
                if named_connection.replaced.is_set():
                    break
                if msg.type == web.WSMsgType.BINARY:
                    try:
                        if audio is not None:
                            session.feed_encoded_audio(audio, msg.data, recording.audio)
                        else:
                            session.feed_audio(msg.data, recording.audio)
                    except OpusError:
                        explicit_stop = True
                        await _fail(ws, "bad_audio", 4400)
                        break
                elif msg.type == web.WSMsgType.TEXT:
                    try:
                        frame = json.loads(msg.data)
                        recording.frame(frame, direction="client")
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(frame, dict):
                        continue
                    if frame.get("type") == "client_diagnostics":
                        diagnostics.respond(frame, msg.data)
                        continue
                    if frame.get("type") == "wake_word_set" and wake_word is not None:
                        wake_word.respond(frame)
                        continue
                    if frame.get("stop") is True:
                        explicit_stop = True
                        _mark_closed(ws, "client")
                        break
                    if frame.get("commit") is True:
                        session.commit()
                    if frame.get("type") == "endpointing":
                        on = frame.get("on")
                        session.set_endpointing(on if isinstance(on, bool) else None,
                                                commit=frame.get("commit") is True)
                    if frame.get("type") == "state":
                        # Leaving `speaking` for a turn: its audio has played or been discarded.
                        if isinstance(frame.get("turn_id"), str) and frame.get("state") != "speaking":
                            session.playback_ended(frame["turn_id"])
                    if (frame.get("type") == "countdown" and attachment is not None
                            and getattr(hub, "countdowns", None) is not None):
                        hub.countdowns.reported(attachment.conversation_id, frame)
                    if frame.get("type") == "approval_response" and attachment is not None:
                        gateway = getattr(hub, "gateway", None)
                        if gateway is not None:
                            gateway.approvals.respond(attachment, frame)
                    if frame.get("type") == "controls":
                        report(frame)  # the device's own mute and volume, for admin connections
                    if frame.get("type") == "cancel":
                        session.cancel(frame.get("turn_id"))
                    if frame.get("type") == "heard":
                        # Neither cancel nor barge-in: the turn carries on unchanged, unless it
                        # called `no_barge_in`, where the wake word is how the person interrupts.
                        session.heard()
                        logger.info("converse heard: session=%s turn=%s wake_word=%r", session.session_id,
                                    getattr(session, "_active_turn_id", None), str(frame.get("wake_word"))[:80])
                    if frame.get("type") == "say":
                        text = _say_text(frame.get("text"))
                        if text:
                            # Fixed text from the device itself (a spoken help reminder): no agent
                            # turn and no history. The client returns to its wake word afterwards.
                            session.transcripts.put(ConversationOutput(
                                text, f"say-{uuid.uuid4().hex}", next_action="standby", transient=True))
                    if frame.get("type") == "commands":
                        listing = getattr(client, "commands", None)  # reads config.yaml: off the loop
                        await _send_json({"type": "commands", "commands": await asyncio.to_thread(listing)}
                                           if listing else {"type": "commands", "error": "failed"})
                    if frame.get("type") == "current_model":
                        await _send_json({"type": "current_model", "current_model": await snapshot()})
                    if frame.get("type") == "command":
                        if frame.get("name") in ("model", "reasoning") and frame.get("args"):
                            if not queue_model_change(frame):
                                await _send_json({"type": "command", "name": frame["name"], "args": frame["args"],
                                                    "error": "change_pending", "current_model": await snapshot()})
                        else:
                            task = asyncio.create_task(_command(frame))
                            wake_tasks.add(task)
                            task.add_done_callback(wake_tasks.discard)
                    if frame.get("type") == "wake":
                        if isinstance(frame.get("claim_id"), str) and frame["claim_id"].strip():
                            session.note_claim(frame["claim_id"])
                        # Do not block PCM, cancel, stop, or a new claim while an election runs.
                        task = asyncio.create_task(_reply_wake(frame))
                        wake_tasks.add(task)
                        task.add_done_callback(wake_tasks.discard)
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                    _mark_closed(ws, "lost", 1006)
                    break
            else:
                # The stream ended: the peer sent a close, or the connection dropped without one.
                code = ws.close_code
                _mark_closed(ws, *(("lost", 1006) if code in (None, 1006) else ("client", code)))
        except Exception:
            _mark_closed(ws, "lost", 1006)
            logger.debug("converse client pump ended", exc_info=True)
        diagnostics.close()
        if wake_word is not None:
            wake_word.close()

    async def _command(frame: dict, *, approved=False) -> None:
        nonlocal dispatched
        name = frame.get("name")
        args = frame.get("args", [])
        reply: dict[str, Any] = {"type": "command", "name": name}
        command: Optional[str] = None
        if "args" in frame:
            reply["args"] = args
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_-]{1,40}", name)
                or not isinstance(args, list) or not all(isinstance(arg, str) for arg in args)):
            reply["error"] = "bad_command"
        elif not hasattr(client, "run_command"):
            reply["error"] = "failed"
        elif (command := await asyncio.to_thread(client.canonical_command, name)) is None:
            reply["error"] = "unknown_command"
        elif command == "stop" and (active_submissions or not client.settled):
            # Hermes itself would interrupt the running agent on dispatch.
            reply["error"] = "busy"
        else:
            if command == "new":
                if model_change and not model_change.done():
                    if not dispatched:
                        model_change.cancel()
                    await asyncio.gather(model_change, return_exceptions=True)
                session.cancel()
            text = f"/{name} {' '.join(args)}".strip()
            previous_session = await asyncio.to_thread(client.session_id) if command == "new" else None
            try:
                if asyncio.current_task() is model_change:
                    if ws.closed or named_connection.replaced.is_set():
                        return
                    dispatched = True
                result = await client.run_command(text, stop=command == "new", approved=approved)
            except Exception:
                logger.warning("converse: command %r failed", text, exc_info=True)
                result = None
            if result is None:
                reply["error"] = "failed"
            else:
                reply["reply"] = result
        if name in ("model", "reasoning"):
            if command == "model" and args and args[0] == "reset" and result is not None:
                await client.apply_voice_default(settings.model, settings.provider)
            reply["current_model"] = (await asyncio.to_thread(client.current_model, default_route)
                                      if asyncio.current_task() is model_change else await snapshot())
        if not ws.closed:
            await _send_json(reply)
            if ("reply" in reply and command == "new"
                    and await asyncio.to_thread(client.session_id) != previous_session):
                # The client reconnects and finds the fresh conversation with a normal start.
                _mark_closed(ws, "server", 4410)
                await ws.close(code=4410, message=b"conversation_changed")

    async def _command_tool(name: str, args: list[str], timeout: float) -> str:
        gateway = getattr(hub, "gateway", None)
        if gateway is None or ws.closed:
            return "not_attached"
        origin = gateway._server_turns.get(attachment.conversation_id)
        if origin is None:
            origin = gateway._turns.get(attachment.conversation_id)
            if origin is not None and (origin.outcome is not None or origin.stopped):
                origin = None
            elif origin is not None:
                # A tool call proves the agent is executing, even when Hermes omitted processing-start.
                # Do not mark merely queued turns active at the handler's admission boundary.
                origin.started = True
        if origin is None:
            return "failed"
        canonical = await asyncio.to_thread(client.canonical_command, name)
        if canonical is None:
            return "unknown_command"
        description = phrase(await asyncio.to_thread(command_catalog.description, canonical))
        question = summary(attachment.assistant_name,
                           f"run a command for you: {description}" + (f", {', '.join(args)}" if args else ""))
        text = f"/{name} {' '.join(args)}".strip()
        if await gateway.approvals.ask(attachment.conversation_id, text, question, timeout) != "once":
            return "denied"
        if getattr(origin, "stopped", False) or getattr(origin, "outcome", None) is not None:
            return "denied"
        if hub.attachment(attachment.conversation_id) is None:
            return "not_attached"
        if canonical in ("model", "reasoning") and args:
            if not queue_model_change({"type": "command", "name": name, "args": args}, approved=True):
                return "change_pending"
            return "Approved. The setting will be applied before the next voice turn."
        if canonical == "stop":
            return "busy"
        if canonical == "new":
            if ws.closed or hub.attachment(attachment.conversation_id) is not attachment:
                return "not_attached"
            async def start_new() -> None:
                if ws.closed or hub.attachment(attachment.conversation_id) is not attachment:
                    return
                if origin.after_playback is not start_new:
                    return
                origin.after_playback = None
                previous_session = await asyncio.to_thread(client.session_id)
                try:
                    result = await client.run_command(text, approved=True)
                except Exception:
                    logger.warning("converse: approved command %r failed after playback", text, exc_info=True)
                    return
                if result is None:
                    logger.warning("converse: approved command %r failed after playback", text)
                elif not ws.closed and await asyncio.to_thread(client.session_id) != previous_session:
                    _mark_closed(ws, "server", 4410)
                    await ws.close(code=4410, message=b"conversation_changed")

            origin.after_playback = start_new
            return "Approved. The new conversation starts when this reply has played."
        try:
            result = await client.run_command(text, stop=False, approved=True)
        except Exception:
            logger.warning("converse: approved command %r failed", text, exc_info=True)
            return "failed"
        return "failed" if result is None else result

    async def _after_turn() -> None:
        nonlocal after_reply
        action, after_reply = after_reply, None
        if action is not None:
            await action()

    async def _run_turn(transcript: str, on_delta, *, interrupted: bool) -> tuple[str, Optional[str]]:
        if model_change and not model_change.done():
            await asyncio.shield(asyncio.gather(model_change, return_exceptions=True))
        finished = loop.create_future()
        active_submissions.add(finished)
        try:
            return await _submit_turn(transcript, on_delta)
        finally:
            finished.set_result(None)
            active_submissions.discard(finished)

    async def _submit_turn(transcript, on_delta):
        nonlocal after_reply
        after_reply = None
        commands = await asyncio.to_thread(client.commands) if hasattr(client, "commands") else None
        state = await snapshot()
        result = await client.run_turn(
            text=transcript, on_delta=on_delta, turn_id=getattr(session, "_active_turn_id", None),
            instructions=voice_system_prompt(
                guidance, name,
                device_name=named_device.device_name, recipient_id=named_device.recipient_id, commands=commands,
                current_model=state))
        after_reply = getattr(result, "after_playback", None)
        return result.reply, result.error

    def _interrupt_turn() -> None:
        # `cancel`: stop the turn in flight.
        gateway = getattr(hub, "gateway", None)
        if gateway is not None and attachment is not None:
            gateway.approvals.on_stop(attachment.conversation_id)
        loop.create_task(client.stop())

    # A replacement socket keeps this driver, but its turns are its own: each connection
    # publishes its runner, and a turn ends with the after-turn hook of the runner it used.
    control["turn"] = (_run_turn, _after_turn)

    async def _run_current_turn(*args, **kwargs):
        control["running"] = control["turn"]
        return await control["running"][0](*args, **kwargs)

    async def _after_current_turn() -> None:
        running = control.pop("running", None)
        if running is not None:
            await running[1]()

    async def _drive_turns() -> None:
        await drive_converse_turns(
            session=session, synth=synth, cap=cap, loop=loop,
            send_json=relay.send_json, send_bytes=relay.send_bytes, run_turn=_run_current_turn, history=history,
            message_ids=MessageIds(client) if hasattr(client, "history") else None,
            record_transcript=relay.record_transcript,
            present=lambda: relay.attached,
            quiet_interval=quiet_interval, interrupt_turn=_interrupt_turn,
            release_turn=getattr(client, "release", None), transcripts=transcripts,
            tool_calls=lambda: client.tool_calls,
            reasoning_tokens=lambda: client.reasoning_tokens,
            last_progress=lambda: client.last_progress,
            turn_timeout=settings.turn_timeout, tail_fallback=settings.tail_fallback,
            max_tts_chars=settings.max_tts_chars,
            standby=lambda: client.standby, mute=lambda: client.mute, after_turn=_after_current_turn,
            control=control,
        )

    detached = False
    pump = None
    driver = adopted.driver if adopted is not None else asyncio.ensure_future(_drive_turns())
    fence = asyncio.create_task(named_connection.replaced.wait())
    try:
        if adopted is not None and (resume := control.get("resume")) is not None:
            await resume(transcripts)
        pump = asyncio.ensure_future(_pump_client())
        done, _ = await asyncio.wait({pump, driver, fence}, return_when=asyncio.FIRST_COMPLETED)
        if driver in done:
            driver.result()  # surface a crash
        elif (not explicit_stop and hub is not None and attachment is not None
              and (not client.settled or control.get("active", lambda: False)())):
            # The connection went away while the agent was working. The turn carries on; its reply
            # is not spoken (nobody is there), and a replacement connection picks up its frames.
            detached = True
        else:
            driver.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await driver
    except (asyncio.CancelledError, RuntimeError, ConnectionResetError):
        # aiohttp may cancel the request handler on disconnect before the pump returns.
        # Preserve the same running turn as on an ordinary socket close.
        detached = (not explicit_stop and hub is not None and attachment is not None
                    and (not client.settled or control.get("active", lambda: False)()) and not driver.done())
    except Exception:
        logger.exception("converse loop crashed")
    finally:
        diagnostics.close()
        if relay.recording is recording:
            relay.recording = None
        recording.close()
        if wake_word is not None:
            wake_word.close()
        if detached:
            relay.ws = None
            control["detached"] = True
            session.stream.discard()
            # Keep detector, playback guard and turn driver together for a replacement socket.
            named_connection.session = None
        else:
            session.stop()
        if pump is not None:
            pump.cancel()
        if not detached:
            driver.cancel()
        for task in wake_tasks:
            if task is not model_change or not dispatched:
                task.cancel()
        fence.cancel()
        await asyncio.gather(*([] if pump is None else [pump]), *([] if detached else [driver]),
                             *wake_tasks, fence, recording.wait_closed(),
                             return_exceptions=True)
        if arbitrator is not None:
            with contextlib.suppress(Exception):
                await arbitrator.unregister(device_id)
        if detached and hub is not None and attachment is not None:
            relay.ws = None
            entry = Detached(relay, client, session, driver, control)
            hub.detached[attachment.conversation_id] = entry

            def forget(finished: asyncio.Future) -> None:
                if not finished.cancelled():
                    finished.exception()  # retrieved: a crash here has nobody to report to
                if hub.detached.get(attachment.conversation_id) is entry:
                    hub.detached.pop(attachment.conversation_id, None)
                    session.stop()
            driver.add_done_callback(forget)
        else:
            with contextlib.suppress(Exception):
                await client.stop()  # `stop` and a turn nobody can finish end the run
        with contextlib.suppress(Exception):
            if named_connection.replaced.is_set():
                _mark_closed(ws, "server", 4409)
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

        route = ConverseRoute(settings_getter=ctx.get_config, base_url_resolver=_base_url, hub=hub,
                              settings_setter=ctx.set_config)
        if route.wire(app):
            logger.info("converse: %s registered on the API server", ROUTE_PATH)

    return factory
