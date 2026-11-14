"""Voice approvals: ``approval_choice: ask`` (specs/server.md, "Approvals").

Hermes owns the request, the choices and the timeout. This module only asks the person, by voice and
by an ``approval_request`` frame, and answers Hermes once with ``resolve_gateway_approval``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

AUX_TIMEOUT = 20.0
WATCH_SECONDS = 1.0
SPEECH_TTL = 60.0

SUMMARY_SYSTEM = (
    "You turn a shell command awaiting permission into one yes-or-no question to read aloud. "
    "Say what will happen in plain words, in twenty words or fewer, ending with a question mark. "
    "Do not read the command out character by character and never repeat secrets. Reply with the question only."
)
ANSWER_SYSTEM = (
    "A voice assistant asked the user a yes-or-no question about running a command. Decide what the user's "
    "reply means. Reply with exactly one word from the allowed list.\n"
    "- once: a plain yes, with nothing more.\n"
    "- session: yes, for the rest of this conversation (for example 'yes, for now').\n"
    "- always: yes, from now on (for example 'always allow that').\n"
    "- deny: no.\n"
    "- unclear: anything else, or you cannot tell.\n"
    "Use session or always only when the user clearly said so."
)


def _aux(system: str, user: str, max_tokens: int) -> str:
    from agent.auxiliary_client import call_llm

    response = call_llm(task="approval", temperature=0, max_tokens=max_tokens, timeout=AUX_TIMEOUT,
                        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    text = (response.choices[0].message.content or "").strip()
    if not text:
        raise ValueError("empty auxiliary reply")
    return text


@dataclass
class _Pending:
    request_id: str
    session_key: str
    chat_id: str
    command: str
    description: str
    choices: list[str]
    summary: str = ""
    device: Any = None  # the Attachment the request was last sent to
    unclear: int = 0
    settled: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: list = field(default_factory=list)


class Approvals:
    """Pending approvals of one gateway adapter, by chat id."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter
        self._pending: dict[str, _Pending] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def _hub(self) -> Any:
        return self._adapter._hub

    # ── a prompt arrives from Hermes ──
    async def begin(self, prompt: Any) -> None:
        choices = [c for c in prompt.choices if c in ("once", "session", "always", "deny")]
        if "deny" not in choices:
            choices.append("deny")
        p = _Pending(str(prompt.request_id or ""), prompt.session_key, prompt.chat_id,
                     str(prompt.command or ""), str(prompt.description or ""), choices)
        if choices == ["deny"] or not p.request_id:
            self._resolve(p, "deny")
            return
        self._loop = asyncio.get_running_loop()
        old = self._pending.get(p.chat_id)
        if old is not None:
            self._settle(old, "deny", "denied")  # Hermes asks one at a time; an older one is stale
        self._pending[p.chat_id] = p
        p.tasks = [asyncio.ensure_future(self._prepare(p)), asyncio.ensure_future(self._watch(p))]

    async def _prepare(self, p: _Pending) -> None:
        user = f"Command:\n{p.command}\n\nDescription:\n{p.description}"
        try:
            p.summary = await asyncio.to_thread(_aux, SUMMARY_SYSTEM, user, 80)
        except Exception:
            logger.warning("converse: approval summary failed; using Hermes's description", exc_info=True)
            p.summary = p.description or "Run a command that needs permission. OK?"
        if not p.settled:
            self._ask(p)

    async def _watch(self, p: _Pending) -> None:
        """Hermes's own timeout ends the request; notice it. Keep the waiting turn alive meanwhile."""
        from tools.approval import list_gateway_approvals

        while not p.settled:
            await asyncio.sleep(WATCH_SECONDS)
            if p.settled:
                return
            ids = {entry.get("request_id") for entry in list_gateway_approvals(p.session_key)}
            if p.request_id not in ids:
                self._settle(p, None, "expired")
                return
            turn = self._adapter._turns.get(p.chat_id)
            if turn is not None and not turn.detached:
                turn.progress_at = time.monotonic()

    # ── asking ──
    def _ask(self, p: _Pending, *, speech: Optional[str] = None) -> None:
        attachment = self._hub.attachment(p.chat_id)
        if attachment is None:
            p.device = None
            return  # held: nobody can answer; Hermes's timeout ends it
        p.device = attachment
        attachment.session.answer_hook = lambda text, _p=p, _a=attachment: self._spoken(_p, _a, text)
        if speech is None:
            self._send(attachment, {
                "type": "approval_request",
                "turn_id": getattr(attachment.session, "_active_turn_id", None),
                "approval_id": p.request_id, "summary": p.summary, "command": p.command,
                "choices": p.choices})
        self._speak(p, speech or p.summary)

    def _speak(self, p: _Pending, text: str) -> None:
        turn = self._adapter._turns.get(p.chat_id)
        if turn is not None and turn.started and turn.outcome is None and not turn.stopped and not turn.detached:
            turn.on_delta(text + "\n")  # the waiting turn says it, like speak_this
        else:
            self._hub.publish(p.chat_id, text, f"approval:{uuid.uuid4().hex}", expires_at=time.time() + SPEECH_TTL)

    @staticmethod
    def _send(attachment: Any, frame: dict) -> None:
        try:
            if attachment is not None and attachment.send is not None:
                attachment.send(frame)
        except Exception:
            logger.debug("converse: approval frame not sent", exc_info=True)

    def attached(self, attachment: Any) -> None:
        """A device attached to the conversation: a pending request asks again, both ways."""
        p = self._pending.get(attachment.conversation_id)
        if p is not None and not p.settled and p.summary:
            self._ask(p)

    # ── answering ──
    def respond(self, attachment: Any, frame: dict) -> None:
        """``approval_response`` from a device."""
        p = self._pending.get(attachment.conversation_id)
        choice = frame.get("choice")
        if (p is None or p.settled or p.device is not attachment or frame.get("approval_id") != p.request_id
                or not isinstance(choice, str) or choice not in p.choices):
            return
        self._settle(p, choice, "denied" if choice == "deny" else "approved")

    def _spoken(self, p: _Pending, attachment: Any, text: str) -> bool:
        """Session worker thread: captured speech while a request is pending. True = it was an answer."""
        loop = self._loop
        if p.settled or p.device is not attachment or loop is None:
            return False
        asyncio.run_coroutine_threadsafe(self._answer(p, text), loop)
        return True

    async def _answer(self, p: _Pending, text: str) -> None:
        async with p.lock:
            if p.settled:
                return
            try:
                user = (f"Question: {p.summary}\nAllowed words: {', '.join(p.choices)}, unclear\n"
                        f"User's reply: {text}")
                word = await asyncio.to_thread(_aux, ANSWER_SYSTEM, user, 8)
                word = word.strip().strip(".!\"' ").lower()
            except Exception:
                logger.warning("converse: approval answer call failed", exc_info=True)
                word = "unclear"
            if p.settled:
                return
            if word == "deny" or ("deny" in p.choices and word == "no"):
                self._settle(p, "deny", "denied")
            elif word in ("once", "session", "always"):
                allowed = next((c for c in (word, "once") if c in p.choices), "deny")
                self._settle(p, allowed, "denied" if allowed == "deny" else "approved")
            else:
                p.unclear += 1
                if p.unclear >= 2:
                    self._settle(p, "deny", "denied")
                else:
                    self._ask(p, speech="Sorry, I did not catch that. " + p.summary)

    # ── ending ──
    def _resolve(self, p: _Pending, choice: str) -> None:
        from tools.approval import resolve_gateway_approval

        resolve_gateway_approval(p.session_key, choice, request_id=p.request_id)

    def _settle(self, p: _Pending, choice: Optional[str], outcome: str) -> None:
        """Answer Hermes (when *choice*), tell the device, and forget the request. Only once."""
        if p.settled:
            return
        p.settled = True
        if choice is not None:
            self._resolve(p, choice)
        if self._pending.get(p.chat_id) is p:
            del self._pending[p.chat_id]
        device = p.device
        if device is not None:
            if getattr(device.session, "answer_hook", None) is not None:
                device.session.answer_hook = None
            self._send(device, {"type": "approval_resolved", "approval_id": p.request_id, "outcome": outcome})
        for task in p.tasks:
            if task is not asyncio.current_task():
                task.cancel()

    def on_stop(self, chat_id: str) -> bool:
        """The turn is being stopped. True when it must be left running: the device went away while a
        request is pending, so the request is held. Otherwise the stop is a cancellation and denies."""
        p = self._pending.get(chat_id)
        if p is None or p.settled:
            return False
        if self._hub.attachment(chat_id) is None:
            p.device = None
            return True
        self._settle(p, "deny", "cancelled")
        return False
