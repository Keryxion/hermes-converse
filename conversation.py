"""Profile-local conversation output subscriptions; no broadcast or offline outbox."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import threading
import time
from typing import Any, Awaitable, Callable, Optional

import uuid

from .loop import ConversationOutput

ANNOUNCE_TTL = 60.0  # seconds an announcement may wait behind the device's current exchange


@dataclass
class Attachment:
    session: Any
    conversation_id: str = ""
    seen: OrderedDict = field(default_factory=OrderedDict)
    pending: int = 0
    send: Optional[Callable[[dict], Any]] = None  # sends a JSON frame to the device
    assistant_name: str = ""  # the start frame's, which approvals speak as
    command: Optional[Callable[[str, list[str], float], Any]] = None
    diagnostics: Optional[Callable[[], Any]] = None
    wake_word: Optional[Callable[[Optional[str]], Any]] = None  # lists phrases, or switches to one


class ConversationHub:
    """One live recipient per conversation in this profile."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._attachments: dict[str, Attachment] = {}
        # The connected ``converse`` gateway adapter, when this Hermes hosts the platform.
        self.gateway: Any = None
        self.gateway_expected = False  # the platform is registered and will connect
        self.detached: dict[str, Any] = {}  # conversation id -> turn running after its socket left
        self.countdowns: Any = None  # set by countdowns.register_tool

    def attach(self, conversation_id: str, session: Any, assistant_name: str = "") -> Attachment:
        with self._lock:
            if conversation_id in self._attachments:
                raise ValueError("conversation_in_use")
            item = Attachment(session, conversation_id, assistant_name=assistant_name)
            self._attachments[conversation_id] = item
            return item

    def attachment(self, conversation_id: str) -> Optional[Attachment]:
        with self._lock:
            item = self._attachments.get(conversation_id)
            return item if item is not None and not item.session.stopped else None

    def detach(self, conversation_id: str, attachment: Attachment) -> None:
        with self._lock:
            if self._attachments.get(conversation_id) is attachment:
                self._attachments.pop(conversation_id)

    def announce(self, conversation_id: str, message: str) -> bool:
        """Speak *message* on the device attached to the conversation, as prepared speech with the
        announcement tone at its start. It waits behind the device's current exchange and is
        dropped when that takes longer than the TTL."""
        return self.publish(conversation_id, message, f"announce:{uuid.uuid4().hex}",
                            expires_at=time.time() + ANNOUNCE_TTL, transient=True,
                            tones=("announcement",)) == "accepted"

    def announce_all(self, message: str, *, but: str = "") -> int:
        """Announce on every attached device except the conversation *but*; how many accepted."""
        with self._lock:
            targets = [key for key in self._attachments if key != but]
        return sum(self.announce(key, message) for key in targets)

    def publish(self, conversation_id: str, text: str, event_id: str, *,
                expires_at: float, next_action: Optional[str] = None, unheard: bool = False,
                no_barge_in: bool = False,
                after_playback: Optional[Callable[[], Awaitable[None]]] = None,
                transient: bool = False, tones: tuple[str, ...] = ()) -> str:
        with self._lock:
            item = self._attachments.get(conversation_id)
            if item is None or item.session.stopped:
                return "not_attached"
            if expires_at <= time.time():
                return "expired"
            if event_id in item.seen:
                return "duplicate"
            if item.pending >= 32:
                return "busy"
            item.seen[event_id] = None
            if len(item.seen) > 1024:
                item.seen.popitem(last=False)
            item.pending += 1
            output = ConversationOutput(text, event_id, next_action=next_action, unheard=unheard,
                                        transient=transient, no_barge_in=no_barge_in, tones=tones)
            output.after_playback = after_playback
            def consumed():
                with self._lock:
                    item.pending -= 1
            output.on_dequeue = consumed
            output.expires_at = expires_at
            item.session.transcripts.put(output)
            return "accepted"
