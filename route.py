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

Protocol (unchanged from the in-core proposal):
  client → ``{"type":"start", "key":?, "input_rate":?, "output_rate":?, "quiet_interval":?,
             "name":?}`` then binary PCM16 mono frames at ``input_rate`` (30 ms blocks preferred),
           ``{"stop": true}`` to end, ``{"commit": true}`` to force an endpoint,
           ``{"type":"drained","turn_id":?}`` when the reply's last audio has left the speaker
  server → ``ready`` (rates, ``session_id``, clock-skew timestamps), ``transcript``, ``thinking``,
           ``speaking`` + binary PCM, ``interrupted``, ``turn_done`` (``expects_more`` in session
           mode), ``quiet``, ``stop_word``, ``error``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from aiohttp import ClientSession, ClientTimeout, web

from .agent_client import RunsClient
from .auth import offered_key_protocol, verify_api_key
from .loop import ConverseSession, drive_converse_turns, parse_start_config, voice_system_prompt
from .settings import Settings, load_settings

logger = logging.getLogger(__name__)

ROUTE_PATH = "/v1/audio/converse"
VOICE_WS_PROTOCOL = "hermes-voice-v1"
# The client must send its ``start`` frame within this window.
FIRST_FRAME_TIMEOUT = 5.0
DEFAULT_LOOPBACK = "http://127.0.0.1:8642"
# Loopback HTTP: no total deadline (the SSE stream lives as long as a turn), bounded connect, and a
# read gap well past the server's 10 s SSE keepalive.
HTTP_TIMEOUT = ClientTimeout(total=None, connect=5, sock_read=90)


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
    """The route handler with its two seams: how settings are read and where the API server is."""

    def __init__(self, *, settings_getter: Callable[..., Any], base_url_resolver: Callable[[], str],
                 profile_name: str = "default") -> None:
        self._settings_getter = settings_getter
        self._base_url = base_url_resolver
        self._profile_name = profile_name

    def wire(self, app: web.Application) -> None:
        app.router.add_get(ROUTE_PATH, self.handle)

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
            input_rate, output_rate, quiet_interval, name, profile = parse_start_config(
                frame, default_name=settings.name)
            if not self._profile_allowed(profile):
                await _fail(ws, "profile_unsupported", 4400)
                return ws
            try:
                synth, cap, session = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: _resolve_session(settings, input_rate, output_rate, quiet_interval))
            except Exception:
                logger.exception("converse setup failed")
                await _fail(ws, "converse setup failed", 1011)
                return ws
            await ws.send_json({
                "type": "ready",
                "input": {"sample_rate": input_rate, "format": "pcm16", "block_ms": 30},
                "output": {"sample_rate": output_rate, "format": "pcm16"},
                "session_id": session.session_id,
                "server_recv_unix_ms": server_recv_unix_ms,
                "server_send_unix_ms": int(time.time() * 1000),
            })
            client = RunsClient(base_url, api_key, http=http, approval_choice=settings.approval_choice)
            await _serve(ws, session=session, synth=synth, cap=cap, client=client, settings=settings,
                         name=name, quiet_interval=quiet_interval)
        return ws


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
    stop_phrases = settings.stop_phrases

    def _transcribe(wav_path: str) -> str:
        return speech.transcribe_wav(wav_path, model=stt_model, stop_phrases=stop_phrases)

    session = ConverseSession(
        np, input_rate=input_rate, quiet_interval=quiet_interval, transcribe=_transcribe,
        endpoint_model=settings.endpoint_model, endpoint_threshold=settings.endpoint_threshold)
    return synth, cap, session


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
                 client: RunsClient, settings: Settings, name: str, quiet_interval: float) -> None:
    """Run the client pump and the turn driver until the socket ends; always stop the live run."""
    loop = asyncio.get_running_loop()
    session.start()
    # The Runs API continues this conversation across turns by session id; one per socket.
    session_id = f"voice_{uuid.uuid4().hex}"
    history: list[dict[str, str]] = []
    allow_signoff = quiet_interval > 0

    async def _pump_client() -> None:
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
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                    break
        except Exception:
            logger.debug("converse client pump ended", exc_info=True)
        session.stop()

    async def _run_turn(transcript: str, on_delta, *, interrupted: bool) -> tuple[str, Optional[str]]:
        result = await client.run_turn(
            text=transcript, session_id=session_id, on_delta=on_delta,
            instructions=voice_system_prompt(name, allow_signoff=allow_signoff, end_phrases=settings.end_phrases),
            model=settings.model, provider=settings.provider,
            idempotency_key=f"converse-{session_id}-{uuid.uuid4().hex}")
        return result.reply, result.error

    def _interrupt_turn() -> None:
        # Barge-in: stop the in-flight run so the barge utterance isn't queued behind it.
        loop.create_task(client.stop())

    async def _drive_turns() -> None:
        await drive_converse_turns(
            session=session, synth=synth, cap=cap, loop=loop,
            send_json=ws.send_json, send_bytes=ws.send_bytes, run_turn=_run_turn, history=history,
            quiet_interval=quiet_interval, interrupt_turn=_interrupt_turn,
            turn_timeout=settings.turn_timeout, tail_fallback=settings.tail_fallback,
            max_tts_chars=settings.max_tts_chars, stop_phrases=settings.stop_phrases,
            end_phrases=settings.end_phrases)

    pump = asyncio.ensure_future(_pump_client())
    driver = asyncio.ensure_future(_drive_turns())
    try:
        done, _ = await asyncio.wait({pump, driver}, return_when=asyncio.FIRST_COMPLETED)
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
        with contextlib.suppress(Exception):
            await client.stop()  # a socket that drops mid-turn must not orphan the run
        with contextlib.suppress(Exception):
            await ws.close()


def make_platform_factory(ctx: Any) -> Callable[[Any, Any], None]:
    """The ``register_platform_handler("api_server", ...)`` factory: ``factory(app, adapter)``."""

    def factory(app: web.Application, adapter: Any) -> None:
        def _base_url() -> str:
            return loopback_base_url(adapter, str(ctx.get_config("api_base_url", "") or ""))

        route = ConverseRoute(settings_getter=ctx.get_config, base_url_resolver=_base_url,
                              profile_name=getattr(ctx, "profile_name", "default"))
        route.wire(app)
        logger.info("converse: %s registered on the API server", ROUTE_PATH)

    return factory
