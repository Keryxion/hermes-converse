"""Spoken and on-screen approvals for Hermes requests and the voice command tool."""

from __future__ import annotations

import asyncio
import math
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .loop import Aside

logger = logging.getLogger(__name__)

AUX_TIMEOUT = 20.0
WATCH_SECONDS = 1.0
SPEECH_TTL = 60.0

SUMMARY_SYSTEM = (
    "A voice assistant asks permission to run a shell command, aloud, as \"<name> wants to <phrase>. Okay?\". "
    "Write only the phrase: what will happen, in plain words, in twenty words or fewer, as a verb phrase "
    "that follows \"wants to\" (for example: open a network connection to the Kitchen speaker to read its logs). "
    "No name, no question mark. Do not read the command out character by character and never repeat secrets. "
    "Reply with the phrase only."
)
NO_NAME = "The assistant"  # who asks when no device has told its assistant_name
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


_SPOKEN = (("once", "yes"), ("deny", "no"), ("session", "yes for now"), ("always", "always"))


def _answers(choices: Any) -> str:
    """How to answer, naming only what this request allows: "Say yes, no, or always." """
    offered = set(choices or ())
    words = [word for choice, word in _SPOKEN if choice in offered] or ["yes", "no"]
    if len(words) == 1:
        return f"Say {words[0]}."
    return "Say " + ", ".join(words[:-1]) + (", or " if len(words) > 2 else " or ") + words[-1] + "."


def phrase(text: str) -> str:
    """Hermes's description (or the model's phrase) as words that follow "wants to": "Start a new
    session (fresh session ID + history)" -> "start a new session, with fresh session ID + history"."""
    text = " ".join((text or "").split()).rstrip(" .!?;:,")
    text = re.sub(r"\s*\(([^()]*)\)$", r", with \1", text).rstrip(" .!?;:,")
    first = text.split(" ", 1)[0]
    if not (len(first) > 1 and first[:2].isupper()):  # SSH, URL: an acronym keeps its capitals
        text = text[:1].lower() + text[1:]
    return text


def summary(assistant_name: str, what: str) -> str:
    """The spoken question: "Arya wants to <what will happen>. Okay?" """
    return f"{assistant_name or NO_NAME} wants to {what}. Okay?"


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
    # The plugin's own question (the ``command`` tool), not Hermes's: the choice goes here, None
    # when nobody answered by *deadline* (monotonic).
    answer: Optional[asyncio.Future] = None
    deadline: float = 0.0


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

    async def ask(self, chat_id: str, command: str, summary: str, timeout: float) -> Optional[str]:
        """Ask the person about the plugin's own *command* as a Hermes request is asked, whatever
        ``approval_choice`` says, with choices once and deny. Their choice, or None with no answer
        within *timeout* seconds (``approvals.timeout``)."""
        self._loop = asyncio.get_running_loop()
        p = _Pending(f"command-{uuid.uuid4().hex}", "", chat_id, command, summary, ["once", "deny"],
                     summary=summary, answer=self._loop.create_future(), deadline=time.monotonic() + timeout)
        old = self._pending.get(chat_id)
        if old is not None:
            self._settle(old, "deny", "denied")
        self._pending[chat_id] = p
        p.tasks = [asyncio.ensure_future(self._watch(p))]
        self._ask(p)
        try:
            async with asyncio.timeout_at(p.deadline):
                return await p.answer
        except TimeoutError:
            self._settle(p, None, "expired")
            return None
        finally:
            self._settle(p, "deny", "cancelled")  # the tool call went away; nothing if already settled

    async def _prepare(self, p: _Pending) -> None:
        user = f"Command:\n{p.command}\n\nDescription:\n{p.description}"
        try:
            what = phrase(await asyncio.to_thread(_aux, SUMMARY_SYSTEM, user, 80))
        except Exception:
            logger.warning("converse: approval summary failed; using Hermes's description", exc_info=True)
            what = ""
        attachment = self._hub.attachment(p.chat_id)
        p.summary = summary(getattr(attachment, "assistant_name", ""),
                            what or phrase(p.description) or "run a command that needs permission")
        if not p.settled:
            self._ask(p)

    async def _watch(self, p: _Pending) -> None:
        """Hermes's own timeout ends the request (ours: its deadline); notice it. Keep the waiting turn
        alive meanwhile."""
        from tools.approval import list_gateway_approvals

        while not p.settled:
            await asyncio.sleep(WATCH_SECONDS)
            if p.settled:
                return
            if p.answer is not None:
                expired = time.monotonic() >= p.deadline
            else:
                expired = p.request_id not in {entry.get("request_id")
                                               for entry in list_gateway_approvals(p.session_key)}
            if expired:
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
        session = attachment.session
        # Not waiting on the person until this question has played: its audio moves this forward.
        session.playback_until = math.inf
        session.answer_taken = False
        session.answer_hook = lambda text, _p=p, _a=attachment: self._spoken(_p, _a, text)
        if speech is None:
            self._send(attachment, {
                "type": "approval_request",
                "turn_id": getattr(attachment.session, "_active_turn_id", None),
                "approval_id": p.request_id, "summary": p.summary, "command": p.command,
                "choices": p.choices})
            # The tone says a question is coming; it plays before the summary is spoken.
            self._send(attachment, {"type": "tone", "tone": "approval"})
        self._speak(p, self._cue(speech or p.summary, p.choices))

    @staticmethod
    def _cue(text: str, choices: Any = ()) -> str:
        """The summary as a question, with the answers Hermes allows for this request. The
        approval tone has already said that a question is coming."""
        text = " ".join((text or "").split())
        if not text:
            text = "Run a command that needs permission?"
        if not text.endswith(("?", ".", "!")):
            text += "?"
        return f"{text} {_answers(choices)}"

    def _speak(self, p: _Pending, text: str) -> None:
        turn = self._adapter._turns.get(p.chat_id)
        if turn is not None and turn.started and turn.outcome is None and not turn.stopped and not turn.detached:
            turn.on_delta(Aside(text + "\n"))  # the waiting turn says it; it is not part of the reply
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
    def _expired(self, p: _Pending) -> bool:
        if p.answer is not None and time.monotonic() >= p.deadline:
            self._settle(p, None, "expired")
            return True
        return False

    def respond(self, attachment: Any, frame: dict) -> None:
        """``approval_response`` from a device."""
        p = self._pending.get(attachment.conversation_id)
        choice = frame.get("choice")
        if (p is None or p.settled or p.device is not attachment or frame.get("approval_id") != p.request_id
                or not isinstance(choice, str) or choice not in p.choices):
            return
        if not self._expired(p):
            self._settle(p, choice, "denied" if choice == "deny" else "approved")

    def _spoken(self, p: _Pending, attachment: Any, text: str) -> bool:
        """The session: captured speech while a request is pending. True = it was an answer."""
        loop = self._loop
        if p.settled or p.device is not attachment or loop is None:
            return False
        asyncio.run_coroutine_threadsafe(self._answer(p, text), loop)
        return True

    async def _answer(self, p: _Pending, text: str) -> None:
        async with p.lock:
            if p.settled or self._expired(p):
                return
            try:
                user = (f"Question: {p.summary}\nAllowed words: {', '.join(p.choices)}, unclear\n"
                        f"User's reply: {text}")
                word = await asyncio.to_thread(_aux, ANSWER_SYSTEM, user, 8)
                word = word.strip().strip(".!\"' ").lower()
            except Exception:
                logger.warning("converse: approval answer call failed", exc_info=True)
                word = "unclear"
            if p.settled or self._expired(p):
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
        if p.answer is not None:
            if not p.answer.done():
                p.answer.set_result(choice)
        elif choice is not None:
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
