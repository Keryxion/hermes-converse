"""Profile-local conversation output subscriptions; no broadcast or offline outbox."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import threading
import time
from typing import Any, Awaitable, Callable, Optional

from .loop import ConversationOutput
from .named import DeviceStore


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
    recipient_id: str = ""  # the device, whose Do Not Disturb setting applies to this conversation


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
        self.devices = DeviceStore()  # opens devices.db on first use

    def attach(self, conversation_id: str, session: Any, assistant_name: str = "",
               recipient_id: str = "") -> Attachment:
        with self._lock:
            if conversation_id in self._attachments:
                raise ValueError("conversation_in_use")
            item = Attachment(session, conversation_id, assistant_name=assistant_name,
                              recipient_id=recipient_id)
            self._attachments[conversation_id] = item
            return item

    def attachment(self, conversation_id: str) -> Optional[Attachment]:
        with self._lock:
            item = self._attachments.get(conversation_id)
            return item if item is not None and not item.session.stopped else None

    def do_not_disturb(self, conversation_id: str) -> bool:
        """Whether the device attached to this conversation is in Do Not Disturb."""
        item = self.attachment(conversation_id)
        return item is not None and self.devices.do_not_disturb(item.recipient_id)

    def set_do_not_disturb(self, conversation_id: str, on: bool) -> bool:
        """The agent's tool: store the setting for the conversation's device, attached or not,
        and tell the device when it changed. False when the conversation has no device."""
        item = self.attachment(conversation_id)
        recipient = item.recipient_id if item is not None else self.devices.recipient_of(conversation_id)
        if not recipient:
            return False
        if self.devices.set_do_not_disturb(recipient, on):
            with self._lock:
                attached = [a for a in self._attachments.values()
                            if a.recipient_id == recipient and not a.session.stopped and a.send is not None]
            for attachment in attached:
                attachment.send({"type": "do_not_disturb", "on": on})
        return True

    def detach(self, conversation_id: str, attachment: Attachment) -> None:
        with self._lock:
            if self._attachments.get(conversation_id) is attachment:
                self._attachments.pop(conversation_id)

    def publish(self, conversation_id: str, text: str, event_id: str, *,
                expires_at: float, next_action: Optional[str] = None, unheard: bool = False,
                after_playback: Optional[Callable[[], Awaitable[None]]] = None,
                transient: bool = False) -> str:
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
                                        transient=transient)
            output.after_playback = after_playback
            def consumed():
                with self._lock:
                    item.pending -= 1
            output.on_dequeue = consumed
            output.expires_at = expires_at
            item.session.transcripts.put(output)
            return "accepted"
