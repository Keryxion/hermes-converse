"""Pure-text policy for the voice loop: stop phrases (the user ends the exchange) and end phrases
(the assistant signs off). No audio, no config, no Hermes imports — these are the plugin's own
rules, so they are unit-testable with a tuple of phrases."""

from __future__ import annotations

import re
from typing import Any, Iterable


def normalize_phrases(raw: Any, default: Iterable[str]) -> tuple[str, ...]:
    """Lower-cased, stripped phrase tuple from a list / tuple / single string; ``[]`` disables the
    feature (an empty tuple), anything malformed falls back to *default*."""
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, (list, tuple)):
        return tuple(str(p).strip().lower() for p in raw
                     if isinstance(p, (str, int, float)) and str(p).strip())
    return tuple(default)


def is_stop_phrase(transcript: str, stop_phrases: Iterable[str]) -> bool:
    """True when *transcript* is EXACTLY a stop phrase. Deliberately strict: the whole utterance —
    lowercased, surrounding punctuation stripped — must equal a phrase, so "stop doing that and try
    again" still reaches the agent."""
    cleaned = transcript.strip().lower().strip(".,!?;: \t\n\"'") if transcript else ""
    return bool(cleaned) and cleaned in tuple(stop_phrases)


def is_end_phrase(reply: str, end_phrases: Iterable[str]) -> bool:
    """True when the assistant's *reply* ENDS with a sign-off phrase (e.g. "over and out"). Unlike
    a stop phrase this is a TRAILING match — the model naturally speaks it as the last thing in a
    reply ("…lights are on. Over and out.") — lowercased with trailing punctuation stripped."""
    cleaned = reply.strip().lower().rstrip(".,!?;: \t\n\"'") if reply else ""
    if not cleaned:
        return False
    return any(cleaned.endswith(p) for p in end_phrases if p)


def strip_end_phrase(text: str, end_phrases: Iterable[str]) -> str:
    """Remove a TRAILING sign-off phrase (with its adjacent punctuation) from *text* so it is used
    only as an end-of-conversation SIGNAL, not spoken aloud. "Give me a shout. Over and out." ->
    "Give me a shout." Returns *text* unchanged when it doesn't end with a phrase (so mid-text
    mentions are untouched). Applied to the TTS text only; the full reply still drives detection."""
    if not text:
        return text
    for p in end_phrases:
        if not p:
            continue
        # Leading class strips only the separator before the phrase (whitespace, comma, dash) —
        # NOT a period, which terminates the previous sentence and must be kept ("shout. Over
        # and out." -> "shout.").
        m = re.search(r"[\s\-—,;:]*" + re.escape(p) + r"[\s.!?,]*$", text, flags=re.IGNORECASE)
        if m:
            return text[:m.start()].rstrip()
    return text
