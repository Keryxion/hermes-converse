"""Profile-local prose and per-turn context for the Converse channel prompt."""

import json
import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

logger = logging.getLogger(__name__)
DEFAULT_FILE = Path(__file__).with_name("defaults") / "CONVERSE.md"


def load_voice_guidance() -> str:
    """Read the active profile's guidance, installing the editable default on first use."""
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "CONVERSE.md"
    try:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Publish a complete file without replacing one created by a concurrent start or edit.
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=path.parent, prefix=".CONVERSE.md-") as seed:
                seed.write(DEFAULT_FILE.read_bytes())
                seed.flush()
                try:
                    path.hardlink_to(seed.name)
                except FileExistsError:
                    pass
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        logger.warning("Cannot load %s, using bundled Converse guidance: %s", path, exc)
        return DEFAULT_FILE.read_text(encoding="utf-8")


def voice_system_prompt(guidance: str, name: str | None = None, *, device_name: str | None = None,
                        recipient_id: str | None = None, commands: list[dict[str, Any]] | None = None) -> str:
    parts = [guidance] if guidance.strip() else []
    name = (name or "").strip()
    if device_name:
        # Cadence addresses this conversation by its title, capped at 40 characters.
        title = (f"{name}: {device_name}" if name else device_name)[:40].rstrip()
        identity = {"assistant_name": name, "device_name": device_name,
                    "recipient_id": recipient_id, "conversation_name": title}
        parts.append("Voice identity supplied by the server: " + json.dumps(identity, ensure_ascii=False))
    elif name:
        parts.append("Assistant name: " + json.dumps(name, ensure_ascii=False))
    if commands is not None:
        parts.append("Gateway command catalog: " + json.dumps(commands, ensure_ascii=False, separators=(",", ":")))
    return "\n\n".join(parts)
