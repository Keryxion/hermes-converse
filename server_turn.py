"""A gateway turn the server started (a reminder, another message), not one the device spoke."""

from __future__ import annotations

from typing import Awaitable, Callable, Optional


class ServerTurn:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.exclusive = False
        self.standby = False
        self.mute = False
        self.no_barge_in = False
        self.held = False  # its reply was ready under Do Not Disturb and was not spoken
        self.after_playback: Optional[Callable[[], Awaitable[None]]] = None

    def speak(self, text: str) -> None:
        self.exclusive = True
        if text.strip():
            self.spoken.append(text.strip())

    def reply(self, final: str) -> str:
        """What the device says: the reply, or only what speak_this gave once it was called."""
        return "\n".join(self.spoken) if self.exclusive else final
