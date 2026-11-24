"""Spoken and on-screen choices for Hermes notices that leave the person a few commands to go on.

Some Hermes replies stop a conversation and name the only commands that continue it: a turn lost
in a restart pauses it until ``/discard`` or ``/reset``, an overlong history fails every turn until
``/compress`` or ``/new``. A chat user taps one; a voice device cannot type it. This raises such a
notice as a choice (``specs/client.md`` "Choices"), asked like an approval and answered by voice
or from a screen, and runs the chosen command as a side chat (``run_command``), never as a message.

Detection is by family (``FAMILIES``), from what Hermes itself says or stores:

* a lost turn: the authority's own admission rows (``status == "unknown"``), read as Hermes's
  ``/discard`` and ``/new`` read them. Durable, so the choice and its id survive reconnects.
* the other families: the whole reply equals one of Hermes's own notices, the catalog template
  ``agent.i18n.t(key)`` (or, for a notice Hermes does not catalog, its literal text).

Only the family's listed commands are ever offered, and only those the gateway's command registry
runs here. Commands quoted in model text, help listings, paths, and any other command a notice
mentions are never choices; no argument is guessed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from . import command_catalog

logger = logging.getLogger(__name__)

SPEECH_TTL = 60.0
WATCH_SECONDS = 2.0
# Speech must begin at least this long after the device reported the question drained: the
# detector trips some blocks after speech begins, so earlier speech may have started over it.
ONSET_MARGIN_SECONDS = 0.25
NOTICE_SECONDS = 300.0  # a choice raised from a notice; a lost turn's lasts as long as the pause

ANSWER_SYSTEM = (
    "A voice assistant asked the user to pick one of a few commands to continue a conversation. Decide what "
    "the user's reply means. Reply with exactly one word from the allowed list.\n"
    "- a command name: the user clearly picked that command, by its name or by what it does.\n"
    "- neither: the user declined or wants to leave things as they are (for example 'never mind', "
    "'neither', 'not now').\n"
    "- unclear: anything else, including a request or question about something else (for example "
    "'reset the thermostat', 'start the dishwasher', 'what time is it'), or you cannot tell.\n"
    "Pick a command only when the reply answers this question."
)


@dataclass(frozen=True)
class Family:
    """One kind of Hermes notice that only the listed commands get past."""

    name: str
    commands: tuple[str, ...]  # offered in this order, as Hermes names them
    summary: str  # what a screen shows
    catalog_key: str = ""  # agent.i18n key of Hermes's notice
    literal: str = ""  # regex of a notice Hermes does not catalog


LOST_TURN = Family(
    "lost_turn", ("discard", "reset"),
    "The last turn was lost when Hermes restarted. Drop it and continue, or start a fresh conversation?")
FAMILIES = (
    LOST_TURN,
    # session_ingress.pause_notice for a head the preflight refused; not in Hermes's catalog.
    Family("unauthorized_queue", ("reset",),
           "This conversation is paused: a queued message could not be re-authorized. Start a fresh one?",
           literal=(r"⏸️ This conversation is paused: a queued message could not be re-authorized \([a-z_]+\)\. "
                    r"Your message is saved but will not be answered here\. Send /reset to start a fresh "
                    r"conversation, or ask the operator to resume this one\.")),
    Family("context_overflow", ("compress", "new"),
           "This conversation is too long to read. Shorten its history, or start a fresh one?",
           catalog_key="gateway.errors.context_overflow"),
    Family("history_unavailable", ("reset",),
           "This conversation's history is unavailable. Start a fresh one?",
           catalog_key="gateway.errors.history_unavailable"),
    Family("history_unreadable", ("new",),
           "This conversation's history cannot be read. Start a fresh one?",
           catalog_key="gateway.shared.history_unreadable"),
    # Its "/retry fails the same way" is not an offer.
    Family("launch_credentials", ("new",),
           "This chat can no longer use the key it started with. Start a new one?",
           catalog_key="gateway.errors.launch_credentials_unavailable"),
)

_DESCRIPTIONS = {
    "discard": "Drop the lost turn and continue (it is not re-run)",
    "reset": "Start a fresh conversation",
    "new": "Start a fresh conversation",
    "compress": "Shorten the conversation's history",
}


def _template_pattern(template: str) -> Optional[re.Pattern]:
    """Hermes's notice template as a whole-text pattern; each ``{placeholder}`` matches any text."""
    template = (template or "").strip()
    if not template:
        return None
    parts = re.split(r"\{[^{}]*\}", template)
    return re.compile(".*?".join(re.escape(part) for part in parts), re.DOTALL)


def _pattern(family: Family) -> Optional[re.Pattern]:
    if family.literal:
        return re.compile(family.literal, re.DOTALL)
    if not family.catalog_key:
        return None
    try:
        from agent.i18n import t
    except ImportError:
        return None
    template = t(family.catalog_key)  # no arguments: the active language's template, unformatted
    return None if template == family.catalog_key else _template_pattern(template)


def notice_family(text: str) -> Optional[Family]:
    """The family whose Hermes notice is the whole of *text*, else None. A quotation of a notice
    inside other text, or a command merely mentioned, is no notice."""
    text = (text or "").strip()
    if not text:
        return None
    for family in FAMILIES:
        pattern = _pattern(family)
        if pattern is not None and pattern.fullmatch(text):
            return family
    return None


def runnable(names: tuple[str, ...]) -> list[str]:
    """The names the gateway's command registry runs here, in order."""
    return [name for name in names if command_catalog.canonical(name) is not None]


def _spoken_list(names: list[str]) -> str:
    if len(names) == 1:
        return f"Say {names[0]}, or never mind."
    return "Say " + ", ".join(names[:-1]) + " or " + names[-1] + "."


def _outcome(reply: str) -> str:
    """The command reply's first line, as speech: "🗑️ Discarded 1 lost turn(s); ..." """
    for line in (reply or "").splitlines():
        line = line.strip()
        if line:
            return line
    return "Done."


@dataclass
class Pause:
    """A conversation's FIFO stopped on turns lost in a restart."""

    session_id: str
    lost: tuple[tuple[str, int], ...]  # (admission_id, generation)
    saved: int  # queued messages behind it, which run once it is discarded

    @property
    def choice_id(self) -> str:
        key = self.session_id + "|" + ",".join(f"{a}:{g}" for a, g in self.lost)
        return "choice-" + hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass
class _Choice:
    choice_id: str
    chat_id: str
    family: Family
    names: list[str]
    session_key: str
    source: Any
    session_id: Optional[str]  # the route's conversation when raised; a different one ends it
    question: str
    entries: list[dict]
    summary: str
    device: Any = None  # the Attachment it was last sent to
    sent_to: Any = None  # the Attachment ``choice_request`` was last sent to
    heard_at: Optional[float] = None  # monotonic time the question finished playing to the person
    asked: Any = None  # the question's ConversationOutput, while it may still be waiting to be spoken
    unclear: int = 0
    settled: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: list = field(default_factory=list)


class Choices:
    """Pending choices of one gateway adapter, by chat id."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter
        self._pending: dict[str, _Choice] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def _hub(self) -> Any:
        return self._adapter._hub

    def pending(self, chat_id: str) -> Optional[_Choice]:
        choice = self._pending.get(chat_id)
        return choice if choice is not None and not choice.settled else None

    # ── what Hermes says and stores ──
    def _authority(self) -> Any:
        runner = getattr(getattr(self._adapter, "_message_handler", None), "__self__", None)
        runner = runner or getattr(self._adapter, "gateway_runner", None)
        if runner is None:
            return None
        from gateway.session_authorities import active_authority
        return active_authority(runner)

    def _session_id(self, session_key: str) -> Optional[str]:
        store = getattr(self._adapter, "_session_store", None)
        return store.peek_session_id(session_key) if store is not None else None

    def _read_pause(self, session_key: str) -> Optional[Pause]:
        """The route's conversation paused on a lost turn, read as Hermes's own /discard reads it."""
        authority = self._authority()
        sid = self._session_id(session_key)
        if authority is None or not sid:
            return None
        from gateway.session_persistence import authority_persistence
        store = authority_persistence(authority)
        if store is None:
            return None
        sid = authority.logical_owner(sid)
        rows = store.admissions.list(session_id=sid)
        lost = tuple(sorted((row.admission_id, int(row.generation or 0)) for row in rows if row.status == "unknown"))
        if not lost:
            return None
        saved = sum(row.status == "queued" for row in rows)
        return Pause(sid, lost, saved)

    async def pause(self, session_key: str) -> Optional[Pause]:
        try:
            return await asyncio.to_thread(self._read_pause, session_key)
        except Exception:
            logger.debug("converse: pause read failed", exc_info=True)
            return None

    # ── raising ──
    def _lost_turn(self, pause: Pause, chat_id: str, session_key: str, source: Any) -> Optional[_Choice]:
        names = runnable(LOST_TURN.commands)
        if not names:
            return None
        discard = _DESCRIPTIONS["discard"]
        if pause.saved:
            discard += f"; the {pause.saved} saved message{'s' if pause.saved != 1 else ''} behind it then run"
        entries = [{"name": n, "description": discard if n == "discard" else _DESCRIPTIONS[n]} for n in names]
        saved = (f" Dropping it lets the {pause.saved} saved request{'s' if pause.saved != 1 else ''} behind it run."
                 if pause.saved and "discard" in names else "")
        question = ("This conversation is paused: a turn was lost when Hermes restarted, so I did not send "
                    f"your request.{saved} Drop the lost turn and continue, or start a fresh conversation? "
                    + _spoken_list(names))
        return _Choice(pause.choice_id, chat_id, LOST_TURN, names, session_key, source, pause.session_id,
                       question, entries, LOST_TURN.summary)

    def _from_notice(self, family: Family, chat_id: str, session_key: str, source: Any) -> Optional[_Choice]:
        names = runnable(family.commands)
        if not names:
            return None
        entries = [{"name": n, "description": _DESCRIPTIONS.get(n) or command_catalog.description(n)} for n in names]
        return _Choice(f"choice-{uuid.uuid4().hex}", chat_id, family, names, session_key, source,
                       self._session_id(session_key), _spoken_list(names), entries, family.summary)

    def _raise(self, choice: _Choice) -> _Choice:
        """Make *choice* the chat's pending one; an older one is settled ``cancelled``."""
        self._loop = asyncio.get_running_loop()
        old = self.pending(choice.chat_id)
        if old is not None:
            if old.choice_id == choice.choice_id:
                return old
            self._settle(old, "cancelled")
        self._pending[choice.chat_id] = choice
        choice.tasks = [asyncio.ensure_future(self._watch(choice))]
        logger.info("converse choice raised: chat=%s choice=%s family=%s choices=%s",
                    choice.chat_id, choice.choice_id, choice.family.name, ",".join(choice.names))
        return choice

    async def before_request(self, attachment: Any, source: Any, session_key: str, text: str) -> bool:
        """A request is about to go to Hermes. True when it must not: a choice is pending (the speech
        goes to it instead), or the conversation is paused on a lost turn, which raises the choice."""
        choice = self.pending(attachment.conversation_id)
        if choice is None:
            pause = await self.pause(session_key)
            if pause is None:
                return False
            choice = self._lost_turn(pause, attachment.conversation_id, session_key, source)
            if choice is None:
                return False
            choice = self._raise(choice)
        await self._hear(choice, attachment, text, onset=getattr(attachment.session, "capture_onset", 0.0))
        return True

    async def after_reply(self, attachment: Any, source: Any, session_key: str, reply: str, *,
                          streamed: bool) -> bool:
        """A turn's reply has been spoken. When it is a Hermes notice of a family, ask the choice.
        A reply the model streamed is the model's text, never Hermes's notice."""
        if streamed or attachment is None:
            return False
        family = notice_family(reply)
        if family is None:
            return False
        choice = await asyncio.to_thread(self._from_notice, family, attachment.conversation_id, session_key, source)
        if choice is None:
            return False
        self._ask(self._raise(choice), attachment)
        return True

    async def attached(self, attachment: Any, source: Any, session_key: str) -> None:
        """A device attached: a pending choice is sent again with the same id, and a conversation
        paused on a lost turn raises its choice. Neither is spoken: the next activation asks."""
        choice = self.pending(attachment.conversation_id)
        if choice is None:
            pause = await self.pause(session_key)
            if pause is not None:
                choice = self._lost_turn(pause, attachment.conversation_id, session_key, source)
                if choice is not None:
                    choice = self._raise(choice)
        if choice is not None:
            self._offer(choice, attachment)

    # ── asking ──
    def _offer(self, choice: _Choice, attachment: Any) -> None:
        """Send ``choice_request`` (once per device) and take the device's speech for the choice."""
        if choice.sent_to is not attachment:
            choice.sent_to = attachment
            self._send(attachment, {"type": "choice_request", "choice_id": choice.choice_id,
                                    "summary": choice.summary, "choices": choice.entries})
        choice.device = attachment
        session = attachment.session
        session.answer_taken = False
        session.answer_hook = lambda text, _c=choice, _a=attachment: self._spoken(_c, _a, text)

    def _ask(self, choice: _Choice, attachment: Any, *, prefix: str = "") -> None:
        """Ask out loud, as an approval asks: the approval tone, then the question, then listening."""
        self._offer(choice, attachment)
        self._send(attachment, {"type": "tone", "tone": "approval"})
        choice.heard_at = None
        session = attachment.session
        # As an approval: not waiting on the person until this question's audio has played.
        session.playback_until = math.inf

        async def heard(_c=choice, _s=session) -> None:
            # Runs once the device reported this question's audio drained (or its tail timed out),
            # never when it was cut: from now on, speech that begins after this answers it.
            if not _c.settled and _c.device is not None and _c.device.session is _s:
                _c.heard_at = time.monotonic()

        _unask(choice)  # an earlier question not yet spoken is not spoken twice
        self._hub.publish(choice.chat_id, prefix + choice.question, f"choice:{uuid.uuid4().hex}",
                          expires_at=time.time() + SPEECH_TTL, transient=True, next_action="listen",
                          after_playback=heard, on_output=lambda output, _c=choice: setattr(_c, "asked", output))

    def _say(self, choice: _Choice, text: str, *, after_playback=None) -> None:
        self._hub.publish(choice.chat_id, text, f"choice:{uuid.uuid4().hex}", expires_at=time.time() + SPEECH_TTL,
                          transient=True, next_action="standby", after_playback=after_playback)

    @staticmethod
    def _send(attachment: Any, frame: dict) -> None:
        try:
            if attachment is not None and attachment.send is not None:
                attachment.send(frame)
        except Exception:
            logger.debug("converse: choice frame not sent", exc_info=True)

    # ── answering ──
    def _spoken(self, choice: _Choice, attachment: Any, text: str) -> bool:
        """The session: captured speech while a choice is pending. Always taken: speech never becomes
        a request while a conversation waits on a choice."""
        loop = self._loop
        if choice.settled or choice.device is not attachment or loop is None:
            return False
        onset = getattr(attachment.session, "capture_onset", 0.0)
        asyncio.run_coroutine_threadsafe(self._hear(choice, attachment, text, onset=onset), loop)
        return True

    async def _hear(self, choice: _Choice, attachment: Any, text: str, *, onset: float) -> None:
        async with choice.lock:
            if choice.settled:
                return
            choice.device = attachment
            if choice.heard_at is None or onset < choice.heard_at + ONSET_MARGIN_SECONDS:
                # Not an answer: the person had not heard the whole question when they began speaking.
                session = attachment.session
                if choice.heard_at is None and (session.playing() or getattr(session, "_tail_active", False)):
                    return  # the question is still playing; it is answered once it has
                self._ask(choice, attachment)
                return
            word = await self._classify(choice, text)
            if choice.settled:
                return
            if word in choice.names:
                await self._apply(choice, word)
            elif word == "neither":
                self._let_go(choice, "Okay. Nothing changed.")
            else:
                choice.unclear += 1
                if choice.unclear < 2:
                    self._ask(choice, attachment, prefix="Sorry, I did not catch that. ")
                else:
                    self._let_go(choice, "Nothing changed.")

    def _let_go(self, choice: _Choice, text: str) -> None:
        """No command was chosen. A lost turn still pauses the conversation in Hermes, so its choice
        stays for the next activation; any other choice ends, and Hermes's next notice raises it again."""
        choice.unclear = 0
        if choice.family is not LOST_TURN:
            self._settle(choice, "cancelled")
        self._say(choice, text)

    async def _classify(self, choice: _Choice, text: str) -> str:
        """The approval answer call (``approvals._aux``), with this choice's commands as the words."""
        from .approvals import _aux

        commands = "\n".join(f"- {e['name']}: {e['description']}" for e in choice.entries)
        allowed = ", ".join([*choice.names, "neither", "unclear"])
        user = (f"Question: {choice.summary}\nCommands:\n{commands}\nAllowed words: {allowed}\n"
                f"User's reply: {text}")
        try:
            word = (await asyncio.to_thread(_aux, ANSWER_SYSTEM, user, 8)).strip().strip(".!\"' ").lower()
        except Exception:
            logger.warning("converse: choice answer call failed", exc_info=True)
            return "unclear"
        return word if word in (*choice.names, "neither") else "unclear"

    def respond(self, attachment: Any, frame: dict) -> None:
        """``choice_response`` from a device's screen."""
        choice = self.pending(attachment.conversation_id)
        name = frame.get("choice")
        if (choice is None or choice.device is not attachment or frame.get("choice_id") != choice.choice_id
                or not isinstance(name, str) or name not in choice.names or self._loop is None):
            return

        async def tapped() -> None:
            async with choice.lock:
                if not choice.settled:
                    await self._apply(choice, name)

        asyncio.run_coroutine_threadsafe(tapped(), self._loop)

    async def _still(self, choice: _Choice) -> bool:
        """Whether the choice still stands, read again just before its command runs."""
        if choice.family is LOST_TURN:
            pause = await self.pause(choice.session_key)
            return pause is not None and pause.choice_id == choice.choice_id
        return await asyncio.to_thread(self._session_id, choice.session_key) == choice.session_id

    async def _apply(self, choice: _Choice, name: str) -> None:
        """Run the chosen command as the chat's own command (``run_command``), once."""
        if choice.settled or name not in choice.names:
            return
        if not await self._still(choice):
            self._settle(choice, "cancelled")
            self._say(choice, "That is already settled. Nothing changed.")
            return
        if await asyncio.to_thread(command_catalog.canonical, name) is None:
            self._settle(choice, "cancelled")
            return
        before = await asyncio.to_thread(self._session_id, choice.session_key)
        try:
            # The person chose this command: Hermes's confirmation for it is that choice.
            reply = await self._adapter.run_command(choice.source, f"/{name}", approved=True)
        except Exception:
            logger.warning("converse: choice command /%s failed", name, exc_info=True)
            reply = None
        if reply is None:
            if choice.family is LOST_TURN and await self._still(choice):
                self._say(choice, "That did not work. The conversation is still paused.")
                return
            self._settle(choice, "cancelled")
            self._say(choice, "That did not work. Nothing changed.")
            return
        self._settle(choice, name)
        device = choice.device
        after = None
        if await asyncio.to_thread(self._session_id, choice.session_key) != before:
            # A fresh conversation: the device reconnects to it, as after a ``command`` frame.
            reconnect = getattr(device, "reconnect", None) if device is not None else None
            if reconnect is not None:
                async def after():
                    reconnect()
        self._say(choice, _outcome(reply), after_playback=after)

    # ── ending ──
    async def _watch(self, choice: _Choice) -> None:
        """Settle the choice ``cancelled`` when its notice no longer holds: another chat sent the
        command, or the route moved to another conversation."""
        raised = time.monotonic()
        while not choice.settled:
            await asyncio.sleep(WATCH_SECONDS)
            if choice.settled:
                return
            async with choice.lock:  # never while an answer is being applied
                if choice.settled:
                    return
                expired = choice.family is not LOST_TURN and time.monotonic() - raised >= NOTICE_SECONDS
                if expired or not await self._still(choice):
                    self._settle(choice, "cancelled")
                    return

    def _settle(self, choice: _Choice, outcome: str) -> None:
        if choice.settled:
            return
        choice.settled = True
        logger.info("converse choice resolved: chat=%s choice=%s outcome=%s",
                    choice.chat_id, choice.choice_id, outcome)
        _unask(choice)  # answered on a screen before the question was spoken: it is not asked
        if self._pending.get(choice.chat_id) is choice:
            del self._pending[choice.chat_id]
        device = choice.device
        if device is not None:
            if _owned(getattr(device.session, "answer_hook", None), choice):
                device.session.answer_hook = None
            self._send(device, {"type": "choice_resolved", "choice_id": choice.choice_id, "outcome": outcome})
        for task in choice.tasks:
            if task is not asyncio.current_task():
                task.cancel()


def _unask(choice: _Choice) -> None:
    """Expire the choice's question if the device has not started speaking it; the driver skips an
    expired output when it takes it from the queue. One already playing plays on."""
    asked, choice.asked = choice.asked, None
    if asked is not None:
        asked.expires_at = 0.0


def _owned(hook: Any, choice: _Choice) -> bool:
    """Whether the session's answer hook is this choice's (an approval may have replaced it)."""
    defaults = getattr(hook, "__defaults__", None) or ()  # the lambda binds (choice, attachment)
    return bool(defaults) and defaults[0] is choice
