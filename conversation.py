"""Profile-local conversation output subscriptions; no broadcast or offline outbox."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import math
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
        self.do_not_disturb_grace = 300.0  # seconds; set from the settings on each connection
        self._requests: dict[str, float] = {}  # recipient_id -> monotonic end of the person's last request
        self._turned_on: dict[str, float] = {}  # recipient_id -> monotonic time Do Not Disturb was last turned on

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
        """Whether what the server starts for this conversation is held: its device is attached and
        in Do Not Disturb, and the person's last request on it is older than the grace."""
        item = self.attachment(conversation_id)
        if item is None or not self.devices.do_not_disturb(item.recipient_id):
            return False
        with self._lock:
            last = self._requests.get(item.recipient_id)
        return last is None or time.monotonic() - last >= self.do_not_disturb_grace

    def request_ended(self, recipient_id: str, started: float) -> None:
        """A turn the person started on this device, at monotonic *started*, has ended: the grace
        starts now, unless Do Not Disturb was turned on since the turn began."""
        with self._lock:
            if self._turned_on.get(recipient_id, -math.inf) < started:
                self._requests[recipient_id] = time.monotonic()

    def store_do_not_disturb(self, recipient_id: str, on: bool) -> bool:
        """Store the device's setting; blocking. Turning it on ends the grace. True when it changed."""
        if on:
            with self._lock:
                self._turned_on[recipient_id] = time.monotonic()
                self._requests.pop(recipient_id, None)
        return self.devices.set_do_not_disturb(recipient_id, on)

    def set_do_not_disturb(self, conversation_id: str, on: bool) -> bool:
        """The agent's tool: store the setting for the conversation's device, attached or not,
        and tell the device when it changed. False when the conversation has no device."""
        item = self.attachment(conversation_id)
        recipient = item.recipient_id if item is not None else self.devices.recipient_of(conversation_id)
        if not recipient:
            return False
        if self.store_do_not_disturb(recipient, on):
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
