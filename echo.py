"""The reply's echo: what the server knows about a reply that the microphone can pick up again.

A device plays the reply through its speaker while its microphone streams, and what the device's
echo cancellation leaves is speech: the assistant's own. The server cannot know the speaker's
volume, the room, or how well the device cancels, so it measures none of them. It knows the two
things it sent: the audio, so how loud the reply is at each moment, and the text, so what the
reply's echo says. :class:`ReplyLevel` is the first; :func:`says_reply` the second.
"""

from __future__ import annotations

import re
from collections import deque
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, Optional

# How far apart the server's playback clock and the microphone may be: the audio sent for one
# moment comes back from the device's speaker, through its microphone and the network, up to this
# much later. The reply's level "now" is its loudest part within the window.
REFERENCE_WINDOW_SECONDS = 1.0
REFERENCE_AHEAD_SECONDS = 0.1
_PIECE_SECONDS = 0.1

# A transcript shorter than this is never judged by its words: "stop", "never mind" and "okay"
# are requests whatever the reply was saying.
ECHO_MIN_WORDS = 3
# Of every this many words, one may be a word the reply did not say: speech to text mishears
# an echo ("paused" as "acquired"). The first word is never that one: a person answering the
# reply reuses its words behind one of their own ("Yes, stop that too").
ECHO_WORDS_PER_MISS = 4
_SAME_WORD = 0.8


class ReplyLevel:
    """How loud the reply being played is, from the audio sent and when it is due to play."""

    def __init__(self, np: Any) -> None:
        self._np = np
        self._pieces: deque = deque()  # (plays from, plays until, RMS), on time.monotonic()

    def sent(self, pcm: bytes, rate: int, start: float) -> None:
        """Reply PCM16 at *rate*, due to start playing at *start*."""
        np = self._np
        samples = np.frombuffer(pcm[:len(pcm) - len(pcm) % 2], dtype=np.int16)  # a chunk may end mid-sample
        size = max(1, int(rate * _PIECE_SECONDS))
        for offset in range(0, samples.size, size):
            piece = samples[offset:offset + size].astype(np.float64)
            self._pieces.append((start + offset / rate, start + (offset + piece.size) / rate,
                                 float(np.sqrt(np.mean(piece ** 2)))))

    def at(self, now: float) -> Optional[float]:
        """The reply's level a microphone could be hearing at *now*; None when none is due."""
        pieces = self._pieces
        while pieces and pieces[0][1] < now - REFERENCE_WINDOW_SECONDS:
            pieces.popleft()
        level = max((rms for start, _end, rms in pieces if start <= now + REFERENCE_AHEAD_SECONDS),
                    default=0.0)
        return level or None


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower().replace("’", "'"))


@lru_cache(maxsize=4096)
def _same(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 3 and SequenceMatcher(None, a, b).ratio() >= _SAME_WORD)


def _in_order(heard: list[str], said: list[str]) -> int:
    """How many of *heard*'s words appear in *said*, in order."""
    row = [0] * (len(said) + 1)
    for word in heard:
        above, row = row, [0]
        for j, other in enumerate(said):
            row.append(above[j] + 1 if _same(word, other) else max(above[j + 1], row[j]))
    return row[-1]


def says_reply(transcript: str, reply: str) -> bool:
    """True when *transcript* is a stretch of *reply* read back: at least ``ECHO_MIN_WORDS`` words,
    starting with one of the reply's and going on in the reply's order, next to each other there,
    apart from one misheard word in every ``ECHO_WORDS_PER_MISS``."""
    heard, said = _words(transcript), _words(reply)
    if len(heard) < ECHO_MIN_WORDS:
        return False
    needed = len(heard) - len(heard) // ECHO_WORDS_PER_MISS
    span = len(heard) + max(2, len(heard) // 2)  # speech to text drops words of an echo too
    return any(_same(heard[0], word) and 1 + _in_order(heard[1:], said[i + 1:i + span]) >= needed
               for i, word in enumerate(said))
