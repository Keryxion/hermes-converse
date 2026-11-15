"""The ``converse`` gateway platform: a named device's voice turns run in the shared gateway runtime.

The WebSocket route stays on the API server (same URL, same API key). What changes is where a turn
executes: an utterance becomes a ``MessageEvent`` on this adapter, so the conversation is an ordinary
gateway session — the same admission, busy handling, scheduling (Cadence), background-process
notifications, compaction and web UI listing that Telegram or Slack conversations get.

How a turn maps onto the adapter contract:

* Admission: ``handle_message(event)``; ``on_processing_start`` / ``on_processing_complete`` bracket it.
* Speech: ``on_stream_delta`` plugin hooks carry the reply as it is generated. The final ``send``
  (marked ``notify``) completes anything the deltas did not carry, so a non-streaming provider or
  a transformed final is still spoken once.
* Barge-in: ``interrupt_session_activity`` — the runner's interrupt monitor stops the agent.
* Output nobody asked for by voice (a Cadence wake, a finished background process, a
  ``send_message``) arrives through ``send`` with no voice turn in flight and is spoken through the
  hub, exactly like any other proactive output.
* Status chatter (busy acks, progress, error notices) is text for a screen; it is never spoken.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome
from .server_turn import ServerTurn

from .catchup import History
from . import command_catalog

from .approvals import Approvals


logger = logging.getLogger(__name__)

PLATFORM = "converse"
# A proactive reply is only worth speaking while it is fresh; the hub drops it afterwards.
OUTPUT_TTL = 60.0
SILENT = "[SILENT]"
COMMAND_TIMEOUT = 15.0


@dataclass
class TurnResult:
    reply: str
    error: Optional[str]  # what the client should hear about
    status: str  # completed | interrupted | cancelled | failed
    turn_id: Optional[str]


@dataclass
class _VoiceTurn:
    """One utterance's turn in the gateway session."""

    event: MessageEvent
    on_delta: Callable[[str], None]
    session_key: str
    done: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)  # stop() gave up waiting
    started: bool = False
    stopped: bool = False
    outcome: Optional[ProcessingOutcome] = None
    final: Optional[str] = None
    streamed: str = ""  # the current model response's text already handed to TTS
    earlier: str = ""  # the last model response that carried text, once another has started
    exclusive: bool = False  # speak_this was called: only its text is spoken
    standby: bool = False  # the agent called standby
    tool_calls: int = 0
    reasoning_tokens: Optional[int] = None
    progress_at: float = 0.0  # monotonic time the agent last did anything
    detached: bool = False  # held on a pending approval after its device left; its speech is published
    held_text: str = ""
    admitted_id: str = ""  # the gateway's id for this input when its busy queue replayed it


@dataclass
class _Command:
    """A gateway command the plugin sent on the device's behalf; its reply is for the log."""

    event: MessageEvent
    done: asyncio.Event = field(default_factory=asyncio.Event)
    started: bool = False
    reply: Optional[str] = None


class ConverseAdapter(BasePlatformAdapter):
    """Gateway adapter for voice devices attached through ``/v1/audio/converse``."""

    def __init__(self, config: PlatformConfig, hub: Any, *, approval_choice: Callable[[], str]):
        super().__init__(config=config, platform=Platform(PLATFORM))
        self._hub = hub
        self._approval_choice = approval_choice
        self._lock = threading.Lock()  # stream hooks arrive on worker threads
        self._turns: dict[str, _VoiceTurn] = {}  # chat_id -> the voice turn in flight
        self._server_turns: dict[str, ServerTurn] = {}  # chat_id -> the server-started turn in flight
        self._processing: dict[str, int] = {}  # chat_id -> gateway turns being processed
        self._commands: dict[str, _Command] = {}  # chat_id -> the plugin's own command in flight
        self._active: dict[str, str] = {}  # chat_id -> message id the gateway is processing
        # Voice turns the device gave up on that the gateway has not finished: message id -> turn.
        self._abandoned: dict[str, _VoiceTurn] = {}
        self._session_chats: dict[str, str] = {}  # session_id -> chat_id
        self.approvals = Approvals(self)

    # ── lifecycle ──
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._hub.gateway = self
        self._mark_connected()
        self._wire_plugin_handlers(None)
        return True

    async def disconnect(self) -> None:
        if getattr(self._hub, "gateway", None) is self:
            self._hub.gateway = None
        self._mark_disconnected()

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id, "type": "dm", "chat_id": chat_id}

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None  # the socket's own ``thinking`` frames report progress

    def set_message_handler(self, handler) -> None:
        @wraps(handler)
        async def handle(event):
            turn = self._own(event)
            # Idle turns already have an adapter lifecycle. Hermes's shared busy
            # observer calls the handler without one, then sends the result itself.
            if turn is None or turn.started:
                return await handler(event)
            outcome = ProcessingOutcome.FAILURE
            try:
                response = await handler(event)
                if not turn.done.is_set() and not turn.stopped:
                    text, _ttl = self._unwrap_ephemeral(response)
                    self._finish_text(turn, text or "")
                outcome = ProcessingOutcome.SUCCESS
                # The voice callback consumed the final. Do not also publish it as
                # proactive output behind the voice turn that was waiting for it.
                return None
            except asyncio.CancelledError:
                outcome = ProcessingOutcome.CANCELLED
                raise
            finally:
                if not turn.done.is_set():
                    self._complete_voice_turn(turn, outcome)

        # BasePlatformAdapter recognizes the runner's session authority through
        # the bound handler. Preserve that identity so busy inputs still use its
        # durable queue rather than the legacy pending-message slot.
        handle.__self__ = getattr(handler, "__self__", None)
        super().set_message_handler(handle)

    # ── conversation identity ──
    def source_for(self, *, chat_id: str, title: str, recipient_id: str, device_name: str):
        return self.build_source(chat_id=chat_id, chat_name=title, chat_type="dm",
                                 user_id=recipient_id, user_name=device_name)

    async def bind(self, source: Any) -> str:
        """Make sure the gateway conversation for this chat exists; return its session key."""
        store = getattr(self, "_session_store", None)
        if store is None:
            raise RuntimeError("gateway session store unavailable")
        entry = await asyncio.to_thread(store.get_or_create_session, source, touch_activity=False)
        return entry.session_key

    async def set_model(self, source: Any, session_key: str, model: Optional[str],
                        provider: Optional[str]) -> None:
        """Make the conversation use *model* (None = its default) by sending the gateway's own
        ``/model`` command, the same way a chat user would. Its confirmation is not spoken."""
        store = getattr(self, "_session_store", None)
        current = (store.get_model_override(session_key) if store is not None else None) or {}
        if (current.get("model") or None, current.get("provider") or None) == (model or None, provider or None):
            return
        text = "/model reset"
        if model or provider:
            text = "/model " + " ".join(part for part in (model, f"--provider {provider}" if provider else "") if part)
        event = MessageEvent(text=text, message_type=MessageType.COMMAND, source=source,
                             message_id=f"model-{uuid.uuid4().hex}")
        command = _Command(event)
        self._commands[source.chat_id] = command
        try:
            await self.handle_message(event)
            if not event._gateway_accepted:
                logger.warning("converse: the gateway did not accept %r", text)
                return
            await asyncio.wait_for(command.done.wait(), COMMAND_TIMEOUT)
            logger.info("converse: %s -> %s", text, " ".join((command.reply or "").split())[:200])
        except TimeoutError:
            logger.warning("converse: %r did not finish in %.0fs", text, COMMAND_TIMEOUT)
        finally:
            if self._commands.get(source.chat_id) is command:
                self._commands.pop(source.chat_id, None)

    async def run_command(self, source: Any, text: str) -> Optional[str]:
        """Run the gateway slash command *text* for the chat, as a chat user would. Its reply is
        returned, not spoken. None when the gateway did not take it or it did not finish."""
        event = MessageEvent(text=text, message_type=MessageType.COMMAND, source=source,
                             message_id=f"command-{uuid.uuid4().hex}")
        command = _Command(event)
        self._commands[source.chat_id] = command
        try:
            await self.handle_message(event)
            if not event._gateway_accepted:
                logger.warning("converse: the gateway did not accept %r", text)
                return None
            await asyncio.wait_for(command.done.wait(), COMMAND_TIMEOUT)
            logger.info("converse: %s -> %s", text, " ".join((command.reply or "").split())[:200])
            return command.reply or ""
        except TimeoutError:
            logger.warning("converse: %r did not finish in %.0fs", text, COMMAND_TIMEOUT)
            return None
        finally:
            if self._commands.get(source.chat_id) is command:
                self._commands.pop(source.chat_id, None)

    # ── turn bracket ──
    def _own(self, event: MessageEvent) -> Optional[_VoiceTurn]:
        source = getattr(event, "source", None)
        turn = self._turns.get(getattr(source, "chat_id", "") or "")
        if turn is not None:
            if turn.event is event or (event.message_id and event.message_id in (
                    turn.event.message_id, turn.admitted_id)):
                return turn
            # Some Hermes queue paths reconstruct the event with an admission id.
            # Keep ownership of a matching replay until it completes or is abandoned.
            if (not turn.started and not turn.admitted_id and event.message_id
                    and not event.get_command() and event.text == turn.event.text):
                turn.admitted_id = str(event.message_id)
                return turn
        return self._abandoned.get(event.message_id or "")

    def abandon(self, turn: _VoiceTurn) -> None:
        """The device moved on; the gateway still owns this turn. Its late output is not spoken."""
        if turn.done.is_set():
            return
        self._abandoned[str(turn.event.message_id)] = turn
        if turn.admitted_id:
            self._abandoned[turn.admitted_id] = turn
        while len(self._abandoned) > 256:
            self._abandoned.pop(next(iter(self._abandoned)))

    async def on_processing_start(self, event: MessageEvent) -> None:
        chat_id = event.source.chat_id
        with self._lock:
            self._processing[chat_id] = self._processing.get(chat_id, 0) + 1
            self._active[chat_id] = str(event.message_id or "")
        command = self._commands.get(chat_id)
        if command is not None and command.event.message_id == event.message_id:
            command.started = True
            return
        turn = self._own(event)
        if turn is None:
            self._server_turns[chat_id] = ServerTurn()
            return
        turn.started = True
        if turn.stopped:  # cancelled while it waited behind another turn
            await self.interrupt_session_activity(turn.session_key, chat_id)

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        chat_id = event.source.chat_id
        with self._lock:
            left = self._processing.get(chat_id, 0) - 1
            if left > 0:
                self._processing[chat_id] = left
            else:
                self._processing.pop(chat_id, None)
            if self._active.get(chat_id) == str(event.message_id or ""):
                self._active.pop(chat_id, None)
        command = self._commands.get(chat_id)
        if command is not None and command.event.message_id == event.message_id:
            command.done.set()
            return
        turn = self._own(event)
        if turn is None:
            self._server_turns.pop(chat_id, None)
        else:
            self._complete_voice_turn(turn, outcome)

    def _complete_voice_turn(self, turn: _VoiceTurn, outcome: ProcessingOutcome) -> None:
        turn.outcome = outcome
        turn.done.set()
        if turn.detached and turn.held_text.strip():
            self._hub.publish(turn.event.source.chat_id, turn.held_text.strip(), f"held:{uuid.uuid4().hex}",
                              expires_at=time.time() + OUTPUT_TTL)
        for key in (str(turn.event.message_id or ""), turn.admitted_id):
            self._abandoned.pop(key, None)

    # ── outbound ──
    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        notify = bool((metadata or {}).get("notify"))
        command = self._commands.get(chat_id)
        if command is not None and command.started and not command.done.is_set():
            command.reply = content
            return SendResult(success=True, message_id=uuid.uuid4().hex)
        if self._active.get(chat_id) in self._abandoned:
            return SendResult(success=True, message_id=uuid.uuid4().hex)  # nobody is waiting for it
        turn = self._turns.get(chat_id)
        if turn is not None and turn.started and turn.outcome is None:
            # The voice turn's own output: its final completes the streamed speech.
            if notify and not turn.stopped:
                self._finish_text(turn, content or "")
            return SendResult(success=True, message_id=uuid.uuid4().hex)
        if not notify and self._processing.get(chat_id):
            return SendResult(success=True, message_id=uuid.uuid4().hex)  # status text, not speech
        server_turn = self._server_turns.get(chat_id)
        text = (server_turn.reply(content or "") if server_turn is not None else content or "").strip()
        if not text or text.upper() == SILENT:
            return SendResult(success=True, message_id=uuid.uuid4().hex)
        event_id = f"send:{uuid.uuid4().hex}"
        status = self._hub.publish(chat_id, text, event_id, expires_at=time.time() + OUTPUT_TTL)
        if status == "accepted":
            return SendResult(success=True, message_id=event_id)
        # No live device (or it is saturated): report it; nothing is queued for a later connection.
        return SendResult(success=False, error=f"converse device {status}", retryable=status == "busy")

    @staticmethod
    def _finish_text(turn: _VoiceTurn, final: str) -> None:
        """Speak what the stream did not carry. After this the turn's speech is complete."""
        text = final.strip()
        turn.final = final
        if turn.exclusive:
            return
        if not text or text.upper() == SILENT:
            return
        # Another model call can start between the reply's last delta and this send (Hermes's
        # background review does), so the reply may be the response before the current one.
        streamed = (turn.streamed if turn.streamed.strip() else turn.earlier).lstrip()
        if not streamed.strip():
            turn.on_delta(final)
            return
        for said in (streamed, streamed.rstrip()):
            if text.startswith(said):
                if text[len(said):].strip():
                    turn.on_delta(text[len(said):])
                return
        # Otherwise the final was transformed after streaming; what was said stands.

    # ── prompts a voice turn cannot answer ──
    async def _send_exec_approval_prompt(self, prompt: Any) -> SendResult:
        from tools.approval import resolve_gateway_approval

        choice = self._approval_choice()
        if choice == "ask":
            await self.approvals.begin(prompt)
            return SendResult(success=True, message_id=uuid.uuid4().hex)
        resolved = resolve_gateway_approval(prompt.session_key, choice, request_id=prompt.request_id)
        if not resolved and choice != "deny":
            resolve_gateway_approval(prompt.session_key, "deny", request_id=prompt.request_id)
        return SendResult(success=True, message_id=uuid.uuid4().hex)

    async def send_clarify(self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
                           session_key: str, metadata: Optional[dict[str, Any]] = None) -> SendResult:
        from tools.clarify_gateway import resolve_gateway_clarify

        resolve_gateway_clarify(clarify_id, "")  # a voice turn cannot answer a clarify form
        return SendResult(success=True, message_id=uuid.uuid4().hex)

    # ── plugin hooks (worker threads) ──
    def _turn_for_session(self, session_id: Any) -> Optional[_VoiceTurn]:
        if not isinstance(session_id, str) or not session_id:
            return None
        with self._lock:
            chat_id = self._session_chats.get(session_id)
            turns = list(self._turns.items())
        if chat_id is None:
            # First turn of a conversation, or compaction rotated the session id mid-turn.
            store = getattr(self, "_session_store", None)
            for candidate, turn in turns:
                if store is not None and store.peek_session_id(turn.session_key) == session_id:
                    chat_id = candidate
                    with self._lock:
                        self._session_chats[session_id] = candidate
                        if len(self._session_chats) > 1024:
                            self._session_chats.pop(next(iter(self._session_chats)))
                    break
        turn = self._turns.get(chat_id) if chat_id else None
        if turn is None or not turn.started or turn.outcome is not None:
            return None
        return turn

    def on_stream_start(self, *, session_id=None, **_: Any) -> None:
        turn = self._turn_for_session(session_id)
        if turn is not None:
            turn.progress_at = time.monotonic()
            if turn.streamed.strip():
                turn.earlier = turn.streamed
            turn.streamed = ""

    def on_stream_delta(self, *, delta=None, kind="text", session_id=None, **_: Any) -> None:
        turn = self._turn_for_session(session_id)
        if turn is None:
            return
        turn.progress_at = time.monotonic()  # reasoning counts as progress too
        if kind != "text" or not isinstance(delta, str) or not delta:
            return
        if turn.stopped or turn.final is not None or turn.exclusive:
            return
        turn.streamed += delta
        turn.on_delta(delta)

    def on_tool_call(self, *, session_id=None, **_: Any) -> None:
        turn = self._turn_for_session(session_id)
        if turn is not None:
            turn.tool_calls += 1
            turn.progress_at = time.monotonic()

    def on_tool_done(self, *, session_id=None, **_: Any) -> None:
        turn = self._turn_for_session(session_id)
        if turn is not None:
            turn.progress_at = time.monotonic()

    def on_api_request(self, *, session_id=None, usage=None, **_: Any) -> None:
        turn = self._turn_for_session(session_id)
        if turn is None:
            return
        turn.progress_at = time.monotonic()
        count = _reasoning_tokens(usage)
        if count is not None:
            turn.reasoning_tokens = (turn.reasoning_tokens or 0) + count


def _reasoning_tokens(usage: Any) -> Optional[int]:
    """Provider-reported reasoning tokens for one model call; None when not reported."""
    if not isinstance(usage, dict):
        return None
    details = usage.get("completion_tokens_details") or usage.get("output_tokens_details")
    for value in (usage.get("reasoning_tokens"), details.get("reasoning_tokens") if isinstance(details, dict) else None):
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


class GatewayTurns:
    """Run one socket's voice turns in its gateway conversation."""

    def __init__(self, adapter: ConverseAdapter, source: Any, session_key: str, *,
                 settle_timeout: float = 10.0) -> None:
        self._adapter = adapter
        self._source = source
        self._session_key = session_key
        self._chat_id = source.chat_id
        self._settle_timeout = settle_timeout
        self._turn: Optional[_VoiceTurn] = None
        self._model: Optional[tuple[Optional[str], Optional[str]]] = None

    @property
    def history(self) -> History:
        """The device conversation's messages in Hermes's session store."""
        return History(getattr(self._adapter, "_session_store", None), self._session_key)

    @property
    def message_key(self) -> Optional[str]:
        """The platform message id of the turn in flight, which Hermes stores with its user message."""
        return str(self._turn.event.message_id) if self._turn is not None else None

    @property
    def tool_calls(self) -> int:
        return self._turn.tool_calls if self._turn is not None else 0

    @property
    def standby(self) -> bool:
        return self._turn is not None and self._turn.standby

    @property
    def reasoning_tokens(self) -> Optional[int]:
        return self._turn.reasoning_tokens if self._turn is not None else None

    @property
    def last_progress(self) -> float:
        """Monotonic time the turn in flight last made progress; 0 before it has."""
        return self._turn.progress_at if self._turn is not None else 0.0

    @property
    def settled(self) -> bool:
        return self._turn is None or self._turn.done.is_set()

    async def run_turn(self, *, text: str, on_delta: Callable[[str], None],
                       instructions: Optional[str] = None, model: Optional[str] = None,
                       provider: Optional[str] = None) -> TurnResult:
        adapter = self._adapter
        if self._turn is not None:
            # A stopped turn the gateway has not finished. The gateway serializes the session, so
            # this request is admitted behind it instead of refusing the device.
            adapter.abandon(self._turn)
        if self._model != (model, provider):  # the first turn also clears a stale earlier choice
            await adapter.set_model(self._source, self._session_key, model, provider)
            self._model = (model, provider)
        turn_id = uuid.uuid4().hex
        event = MessageEvent(
            text=text, message_type=MessageType.TEXT, source=self._source, message_id=turn_id,
            channel_prompt=instructions,
            # A transcript is speech, never a gateway command.
            allow_gateway_control=False)
        turn = self._turn = _VoiceTurn(event, on_delta, self._session_key)
        adapter._turns[self._chat_id] = turn
        try:
            await adapter.handle_message(event)
            if not event._gateway_accepted and self._session_key not in adapter._active_sessions:
                turn.done.set()
                return TurnResult("", "the gateway did not accept the request", "failed", turn_id)
            waiters = [asyncio.ensure_future(turn.done.wait()), asyncio.ensure_future(turn.released.wait())]
            try:
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in waiters:
                    waiter.cancel()
        except asyncio.CancelledError:
            # The host abandoned the turn (deadline / socket closed): never leave it running.
            await asyncio.shield(self.stop())
            raise
        finally:
            if turn.done.is_set() and adapter._turns.get(self._chat_id) is turn:
                adapter._turns.pop(self._chat_id, None)
        if turn.stopped or not turn.done.is_set():
            return TurnResult(turn.final or "", None, "interrupted", turn_id)
        if turn.outcome is ProcessingOutcome.SUCCESS or turn.final:
            return TurnResult(turn.final or "", None, "completed", turn_id)
        if turn.outcome is ProcessingOutcome.CANCELLED:
            return TurnResult("", "agent run was cancelled", "cancelled", turn_id)
        return TurnResult("", "agent run failed", "failed", turn_id)

    def commands(self) -> list[dict[str, Any]]:
        """The gateway commands this device may name in a ``command`` frame."""
        return command_catalog.catalog()

    def canonical_command(self, name: str) -> Optional[str]:
        """The canonical name of command or alias *name*; None when the gateway has no such command."""
        return command_catalog.canonical(name)

    async def run_command(self, text: str) -> Optional[str]:
        """End the turn in flight, then run the gateway command; its reply, or None on failure."""
        await self.stop()
        self._model = None  # a command such as /new may drop the model override; the next turn sets it again
        return await self._adapter.run_command(self._source, text)

    async def stop(self) -> None:
        """Interrupt the turn in flight (barge-in, socket close) and wait — bounded — for it to settle."""
        turn = self._turn
        if turn is None or turn.done.is_set():
            return
        if self._adapter.approvals.on_stop(self._chat_id):
            # The device left with an approval pending: the turn keeps waiting for it.
            turn.detached = True
            turn.on_delta = lambda text, _t=turn: setattr(_t, "held_text", _t.held_text + text)
            return
        turn.stopped = True
        if turn.started:
            await self._adapter.interrupt_session_activity(self._session_key, self._chat_id)
        try:
            await asyncio.wait_for(turn.done.wait(), self._settle_timeout)
        except TimeoutError:
            logger.warning("converse: turn %s did not settle after stop; detaching", turn.event.message_id)
            self._adapter.abandon(turn)
            turn.released.set()


def register_platform(ctx: Any, hub: Any) -> bool:
    """Register the platform and its stream hooks. False when this Hermes cannot host it."""
    from .settings import load_settings

    def factory(config: PlatformConfig) -> ConverseAdapter:
        return ConverseAdapter(config, hub,
                               approval_choice=lambda: load_settings(ctx.get_config).approval_choice)

    try:
        ctx.register_platform(
            name=PLATFORM, label="Converse", adapter_factory=factory,
            check_fn=lambda: True, is_connected=lambda config: True,
            # Every event comes from a socket that already presented the API key.
            trusted_inbound=True,
            # Text status is for a screen. Speech comes from the stream hooks and the final reply.
            display_tier="minimal",
            display_overrides={"streaming": False, "busy_steer_ack_enabled": False},
            emoji="🎙️", allow_update_command=False, pii_safe=True)
    except (AttributeError, TypeError):
        logger.warning("converse: this Hermes cannot register the converse platform", exc_info=True)
        return False

    def forward(name: str) -> Callable[..., None]:
        def hook(**kwargs: Any) -> None:
            adapter = getattr(hub, "gateway", None)
            if adapter is not None:
                getattr(adapter, name)(**kwargs)
        return hook

    hub.gateway_expected = True
    ctx.register_hook("on_stream_start", forward("on_stream_start"))
    ctx.register_hook("on_stream_delta", forward("on_stream_delta"))
    ctx.register_hook("pre_tool_call", forward("on_tool_call"))
    ctx.register_hook("post_tool_call", forward("on_tool_done"))
    ctx.register_hook("post_api_request", forward("on_api_request"))
    return True
