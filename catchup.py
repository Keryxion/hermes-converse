"""Reading a device conversation's messages from Hermes: message ids and catching up.

Nothing is stored here. Hermes's session store has the conversation's messages; this module reads
them to name the messages live transcripts refer to and to replay what a client missed.
A turn that outlives its socket keeps running; :class:`Relay` is where its frames go meanwhile.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from . import speech
from .audio_transport import on_audio_thread

logger = logging.getLogger(__name__)

REPLAY_AFTER_LIMIT = 100
REPLAY_FRESH_LIMIT = 20
SILENT = "[SILENT]"
# What Hermes stores as the reply of a turn it interrupted ("Operation interrupted: waiting for
# model response (10.6s elapsed)."). It was never said, so it is not replayed.
INTERRUPT_NOTICE = "Operation interrupted"


class History:
    """The messages of one gateway conversation, read from Hermes's session store."""

    def __init__(self, store: Any, session_key: str) -> None:
        self._store, self._session_key = store, session_key

    def rows(self) -> Optional[list[dict[str, Any]]]:
        """Active messages in order, including those compaction folded away and earlier segments.
        None when Hermes's messages cannot be read."""
        store = self._store
        try:
            session_id = store.peek_session_id(self._session_key) if store is not None else None
            if not session_id:
                return []
            return list(store._db_for_session_id(session_id).get_messages(
                session_id, include_compacted=True, include_ancestors=True))
        except Exception:
            logger.warning("converse: could not read the conversation's messages", exc_info=True)
            return None


class MessageIds:
    """Hermes's ids for the messages of the turn a client is running."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def _find(self) -> tuple[Optional[dict], Optional[int], bool]:
        key = self._client.message_key
        user = assistant_id = None
        rows = self._client.history.rows()
        for row in rows or []:
            if user is None:
                if key and row.get("platform_message_id") == key and row.get("role") == "user":
                    user = row
            elif row.get("role") == "assistant":
                assistant_id = row["id"]
            elif row.get("role") == "user":
                break
        return user, assistant_id, rows is not None

    async def user_message(self, settled: asyncio.Event, timeout: float = 5.0) -> Optional[tuple[str, str]]:
        """The stored user message's id and text. Hermes writes it as the agent starts, so wait for
        it briefly; once the turn has *settled*, one last look decides. None when it was not stored."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            last = settled.is_set() or loop.time() >= deadline
            row, _, readable = await loop.run_in_executor(None, self._find)
            if row is not None:
                content = row.get("content")
                return str(row["id"]), content if isinstance(content, str) else ""
            if not readable or last:
                return None
            await asyncio.sleep(0.1)

    async def output_id(self, text: str) -> Optional[str]:
        """The id of the stored reply a server-started turn just spoke: the latest assistant
        message that says *text*, else the latest one."""
        rows = await asyncio.get_running_loop().run_in_executor(None, self._client.history.rows)
        replies = [f for f in replay_frames(rows or [], None) if f["role"] == "assistant"]
        said = " ".join(text.split())
        for frame in reversed(replies):
            if " ".join(frame["text"].split()) == said:
                return frame["message_id"]
        return replies[-1]["message_id"] if replies else None

    async def assistant_id(self) -> Optional[str]:
        """The id of the turn's last assistant message, once the turn has settled."""
        _, found, _ = await asyncio.get_running_loop().run_in_executor(None, self._find)
        return None if found is None else str(found)


def _spoken_text(rows: list[dict[str, Any]]) -> str:
    """What was submitted for speech in a turn's assistant messages: the text given to
    ``speak_this`` when it was used, otherwise the reply text."""
    tool_texts, used = [], False
    for row in rows:
        for call in row.get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name") != "speak_this":
                continue
            used = True
            try:
                text = json.loads(function.get("arguments") or "{}").get("text")
            except (ValueError, AttributeError):
                continue
            if isinstance(text, str):
                tool_texts.append(text)
    texts = tool_texts if used else [r["content"] for r in rows if isinstance(r.get("content"), str)
                                     and not r["content"].lstrip().startswith(INTERRUPT_NOTICE)]
    cleaned = [speech.strip_markdown(text).strip() for text in texts]
    spoken = " ".join(text for text in cleaned if text)
    return "" if spoken.upper() == SILENT else spoken


def replay_frames(rows: list[dict[str, Any]], after: Optional[str]) -> list[dict[str, Any]]:
    """The ``transcript`` frames a client missed: user and assistant messages after *after*
    (at most the latest 100), or the latest 20 without it or when it is not in this conversation."""
    groups: list[tuple[Optional[dict], list[dict]]] = []
    for row in rows:
        role = row.get("role")
        if role == "user":
            groups.append((row, []))
        elif role == "assistant":
            if not groups:
                groups.append((None, []))
            groups[-1][1].append(row)
    frames: list[tuple[int, dict[str, Any]]] = []
    for user, assistants in groups:
        turn_id = user.get("platform_message_id") if user else None
        extra = {"turn_id": turn_id} if isinstance(turn_id, str) and turn_id else {}
        if user is not None and not user.get("display_kind") and isinstance(user.get("content"), str) \
                and user["content"].strip():
            frames.append((user["id"], {"type": "transcript", "message_id": str(user["id"]),
                                        "role": "user", **extra, "text": user["content"],
                                        "replay": True}))
        text = _spoken_text(assistants)
        if text and assistants:
            last = assistants[-1]["id"]
            frames.append((last, {"type": "transcript", "message_id": str(last),
                                  "role": "assistant", **extra, "text": text, "replay": True}))
    known = {row["id"] for row in rows}
    anchor = int(after) if isinstance(after, str) and after.isdigit() else None
    if anchor is not None and anchor in known:
        return [frame for number, frame in frames if number > anchor][-REPLAY_AFTER_LIMIT:]
    return [frame for _, frame in frames][-REPLAY_FRESH_LIMIT:]


class Relay:
    """Where one device conversation's turn frames go: the connected socket, or nowhere."""

    def __init__(self, ws: Any, *, audio=None, source_rate=None) -> None:
        self.ws = ws
        self.audio, self.source_rate = audio, source_rate

    @property
    def attached(self) -> bool:
        return self.ws is not None

    def attach(self, ws: Any, *, audio=None) -> None:
        self.ws = ws
        self.audio = audio

    async def _send(self, method: str, payload: Any) -> None:
        ws = self.ws
        if ws is None:
            return
        try:
            await getattr(ws, method)(payload)
        except ConnectionResetError:
            # The reader will detach the running turn. A simultaneous write failure must not
            # cancel it first, especially while the agent is waiting for an approval answer.
            if self.ws is ws:
                self.ws = None

    async def send_json(self, frame: dict) -> None:
        if self.ws is not None:
            audio = self.audio
            if audio is not None and _changes_audio(frame):
                # The Opus state is the audio thread's: every change to it runs there, in order.
                for packet in await on_audio_thread(_audio_frame, audio, frame):
                    if self.ws is None:
                        return
                    await self._send("send_bytes", packet)
                if self.ws is None:
                    return
            await self._send("send_json", frame)

    async def send_bytes(self, data: bytes) -> None:
        if self.ws is not None:
            audio = self.audio
            if audio is None:
                await self._send("send_bytes", data)
            else:
                for packet in await on_audio_thread(lambda: list(audio.encode(data, self.source_rate))):
                    if self.ws is None:
                        return
                    await self._send("send_bytes", packet)


def _changes_audio(frame: dict) -> bool:
    kind = frame.get("type")
    return (kind in ("speaking", "interrupted", "error", "turn_done")
            or kind == "state" and frame.get("state") == "listening")


def _audio_frame(audio: Any, frame: dict) -> list[bytes]:
    """The Opus state change a control frame makes; returns the packets to send before it."""
    if frame.get("type") == "speaking":
        audio.begin(frame.get("turn_id"))
    elif frame.get("type") in ("interrupted", "error"):
        audio.discard()
    else:
        return list(audio.finish())
    return []


@dataclass
class Detached:
    """A turn still running after its socket went away, waiting for a replacement connection."""

    relay: Relay
    client: Any  # the GatewayTurns running it
    session: Any  # the dropped connection's session, which can still cancel the turn
    driver: asyncio.Future
    control: Any = None  # the driver's hooks: `resume` tells a new connection where the turn stands
