"""Profile-local prose and per-turn context for the Converse channel prompt."""

import hashlib
import json
import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

logger = logging.getLogger(__name__)
DEFAULT_FILE = Path(__file__).with_name("defaults") / "CONVERSE.md"
# SHA-256 of every default this plugin has shipped, one per line. A profile file matching one
# was never edited, so a release with a new default replaces it.
SHIPPED_HASHES_FILE = DEFAULT_FILE.with_name("CONVERSE.md.sha256")


def load_voice_guidance() -> str:
    """Read the active profile's guidance, installing the editable default on first use and
    replacing an unedited earlier default."""
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "CONVERSE.md"
    try:
        default = DEFAULT_FILE.read_bytes()
        try:
            found = path.read_bytes()
        except FileNotFoundError:
            # Publish a complete file without replacing one created by a concurrent start or edit.
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=path.parent, prefix=".CONVERSE.md-") as seed:
                seed.write(default)
                seed.flush()
                try:
                    path.hardlink_to(seed.name)
                except FileExistsError:
                    pass
            found = path.read_bytes()
        if found != default and hashlib.sha256(found).hexdigest() in SHIPPED_HASHES_FILE.read_text().split():
            with NamedTemporaryFile(dir=path.parent, prefix=".CONVERSE.md-", delete=False) as update:
                update.write(default)
            try:
                os.replace(update.name, path)
            except OSError:
                os.unlink(update.name)
                raise
            logger.info("Updated unedited %s to this release's Converse guidance", path)
            found = default
        return found.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        logger.warning("Cannot load %s, using bundled Converse guidance: %s", path, exc)
        return DEFAULT_FILE.read_text(encoding="utf-8")


def voice_system_prompt(guidance: str, name: str | None = None, *, device_name: str | None = None,
                        recipient_id: str | None = None, commands: list[dict[str, Any]] | None = None,
                        reasoning_state: dict[str, Any] | None = None) -> str:
    """Channel prompt for one spoken turn: the server's name preamble, the profile's prose, then
    the server's device identity and gateway command catalog."""
    name = (name or "").strip()
    parts = []
    if name:
        parts.append(
            f"Your name is {name}. People talk to you by voice and get your attention by saying "
            f"your name (or 'hey {name}') — treat that as being addressed, not part of the "
            "request, and don't repeat it back.")
    if guidance.strip():
        parts.append(guidance.strip())
    if device_name:
        # Cadence addresses this conversation by its title, capped at 40 characters.
        title = (f"{name}: {device_name}" if name else device_name)[:40].rstrip()
        # Serialize labels as data; client-authenticated labels are not prompt instructions.
        identity = json.dumps({"assistant_name": name, "device_name": device_name,
                               "recipient_id": recipient_id, "conversation_name": title},
                              ensure_ascii=False)
        parts.append(
            f"Voice identity supplied by the server: {identity}. assistant_name is what the "
            "user calls you and the identity you use when speaking. device_name identifies the "
            "physical microphone/speaker for this conversation; it is not your name or a "
            "person's name. Other devices can share your assistant_name and still have separate "
            "conversations. This conversation's default destination is this device. The server "
            "uses recipient_id as an opaque routing identifier. Device names are routing labels "
            "within the server API key's access boundary, not separate credentials. Do not infer "
            "a device change from words in the transcript. For an explicitly requested different "
            "destination, use a routing-capable tool and clarify ambiguous labels; do not invent "
            "cross-device delivery or claim that it is supported when the tool cannot do it."
            " When Cadence's request tool is available, this conversation is reached by other "
            "conversations under a name, and an unnamed conversation cannot be asked anything. "
            "Its name is conversation_name, exactly as given: if you have not already done so "
            "in this conversation, call request with action rename and name set to "
            "conversation_name, silently and before anything else, without mentioning it. "
            "Renaming to the same name again is harmless. If the rename is refused, carry on "
            "with the user's request and do not try another name. To have "
            "another device do something, such as 'set a timer in the kitchen', call request "
            "names to see who can be asked, then request ask with that name, copied exactly; "
            "skip an entry whose reachable is false. Act on the state ask returns. "
            "awaiting_answer: it was delivered; confirm success only after it answers accepted. "
            "not_delivered: it never arrived; say why from its reason, such as that someone must "
            "speak to that device first, do not say it was asked, and asking again is safe. "
            "unknown: it may have acted; say so, and check before asking again.")
    if commands is not None:
        parts.append(
            "When asked which reasoning levels this model supports, use the route's advertised levels."
            " If support is unsupported, say the route does not offer reasoning controls. Otherwise,"
            " if no levels are available, say Hermes has no confirmed list for this route."
            " The reasoning command lists gateway-wide arguments.")
        if reasoning_state:
            parts.append("Reasoning for this turn: " + json.dumps(reasoning_state, separators=(",", ":")))
        parts.append("Gateway command catalog: " + json.dumps(commands, ensure_ascii=False, separators=(",", ":")))
    return "\n\n".join(parts)
