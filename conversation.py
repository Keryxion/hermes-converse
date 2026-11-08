"""Profile-local conversation output subscriptions; no broadcast or offline outbox."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
import threading
import time
from typing import Any

from .loop import ConversationOutput


@dataclass
class VoiceRequest:
    text: str
    reply: str | None = None
    cancelled: bool = False
    candidates: OrderedDict[str, tuple[str, float]] = field(default_factory=OrderedDict)


@dataclass
class Attachment:
    session: Any
    seen: OrderedDict = field(default_factory=OrderedDict)
    voice_request: VoiceRequest | None = None
    pending: int = 0


class ConversationHub:
    """One live recipient per conversation in this profile; hooks may arrive on worker threads."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._attachments: dict[str, Attachment] = {}

    def attach(self, conversation_id: str, session: Any) -> Attachment:
        with self._lock:
            if conversation_id in self._attachments:
                raise ValueError("conversation_in_use")
            item = Attachment(session)
            self._attachments[conversation_id] = item
            return item

    def detach(self, conversation_id: str, attachment: Attachment) -> None:
        with self._lock:
            if self._attachments.get(conversation_id) is attachment:
                self._attachments.pop(conversation_id)

    @contextmanager
    def owned_run(self, attachment: Attachment, text: str):
        request = VoiceRequest(text)
        with self._lock:
            attachment.voice_request = request
        try:
            yield request
        except asyncio.CancelledError:
            request.cancelled = True
            raise
        finally:
            with self._lock:
                attachment.voice_request = None
                candidates = list(request.candidates.items())
            # The public hook identifies a conversation/turn, not the API run. Match the
            # final text as well as its user input; never drop unrelated background replies.
            own_event = next((event_id for event_id, response in reversed(candidates)
                              if response[0] == request.reply), None)
            for event_id, (response, expiry) in candidates:
                if not request.cancelled and event_id != own_event:
                    self.publish(attachment.session.session_id, response, event_id, expires_at=expiry)
                else:
                    # Only confirmed duplicates or explicitly cancelled requests are suppressed.
                    # A failed run with no reply cannot identify any candidate as its duplicate.
                    with self._lock:
                        attachment.seen[event_id] = None
                        if len(attachment.seen) > 1024:
                            attachment.seen.popitem(last=False)

    def publish(self, conversation_id: str, text: str, event_id: str, *,
                expires_at: float, next_action: str = "standby") -> str:
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
            output = ConversationOutput(text, event_id, next_action=next_action)
            def consumed():
                with self._lock:
                    item.pending -= 1
            output.on_dequeue = consumed
            output.expires_at = expires_at
            item.session.transcripts.put(output)
            return "accepted"

    def on_response(self, *, session_id=None, turn_id=None, assistant_response=None,
                    user_message=None, **kwargs):
        """Observe final, transformed conversation responses, not raw token/reasoning streams.

        Voice-owned runs already have a streaming TTS path and must never be spoken twice.
        Scheduling and notification-policy decisions stay with the producing integration.
        """
        if not isinstance(session_id, str) or not isinstance(turn_id, str):
            return
        if (not isinstance(assistant_response, str) or not assistant_response.strip()
                or assistant_response.strip().upper() == "[SILENT]"):
            return
        if len(assistant_response) > 20000:
            return
        event_id = f"turn:{turn_id}"
        with self._lock:
            item = self._attachments.get(session_id)
            request = item.voice_request if item is not None else None
            if request is not None and user_message == request.text:
                if len(request.candidates) < 32 or event_id in request.candidates:
                    request.candidates[event_id] = (assistant_response, time.time() + 60)
                return
        self.publish(session_id, assistant_response, event_id, expires_at=time.time() + 60)
