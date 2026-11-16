"""Profile-local conversation output subscriptions; no broadcast or offline outbox."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import threading
import time
from typing import Any, Callable, Optional

from .loop import ConversationOutput


@dataclass
class Attachment:
    session: Any
    conversation_id: str = ""
    seen: OrderedDict = field(default_factory=OrderedDict)
    pending: int = 0
    send: Optional[Callable[[dict], Any]] = None  # sends a JSON frame to the device
    assistant_name: str = ""  # the start frame's, which approvals speak as
    # The ``command`` tool: (text, approval timeout) -> a concurrent future of its result, on the socket's loop.
    command: Optional[Callable[[str, float], Any]] = None


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

    def publish(self, conversation_id: str, text: str, event_id: str, *,
                expires_at: float, next_action: Optional[str] = None, unheard: bool = False) -> str:
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
            output = ConversationOutput(text, event_id, next_action=next_action, unheard=unheard)
            def consumed():
                with self._lock:
                    item.pending -= 1
            output.on_dequeue = consumed
            output.expires_at = expires_at
            item.session.transcripts.put(output)
            return "accepted"
