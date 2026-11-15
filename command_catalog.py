"""The gateway commands a device may name in a ``command`` frame, read from Hermes's own registry.

Hermes builds Telegram's menu and Slack's manifest from the same registry. A command that only
runs in the terminal is left out; so is one the platform's slash-access policy would refuse the
device. Arguments have no schema in Hermes: ``args_hint`` (such as ``[name]``) and ``subcommands``
are all it knows, so ``args`` stays free text the command parses.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from hermes_cli.commands import COMMAND_REGISTRY, SUBCOMMANDS, resolve_command

logger = logging.getLogger(__name__)


def _gateway_available() -> list[Any]:
    try:  # Private helpers; gateway_help_lines uses the same pair to decide what /help shows.
        from hermes_cli.commands import _is_gateway_available, _resolve_config_gates
        gates = _resolve_config_gates()
        return [cmd for cmd in COMMAND_REGISTRY if _is_gateway_available(cmd, gates)]
    except ImportError:
        return [cmd for cmd in COMMAND_REGISTRY if not cmd.cli_only]


def _plugin_commands() -> dict[str, dict]:
    try:
        from hermes_cli.plugins import get_plugin_commands
        return {name: meta for name, meta in (get_plugin_commands() or {}).items()
                if isinstance(name, str) and isinstance(meta, dict)}
    except Exception:
        logger.debug("converse: plugin commands unavailable", exc_info=True)
        return {}


def _policy(extra: Any):
    try:
        from gateway.slash_access import policy_from_extra
        return policy_from_extra(extra if isinstance(extra, dict) else {}, "dm")
    except ImportError:
        return None


def canonical(name: str) -> Optional[str]:
    """The canonical name of a command or alias the gateway runs; None when there is no such command."""
    cmd = resolve_command(name)
    if cmd is not None:
        return cmd.name if cmd in _gateway_available() else None
    return name if name in _plugin_commands() else None


def catalog(extra: Any, user_id: Optional[str]) -> list[dict[str, Any]]:
    """Every command the device may run, in registry order, then plugin commands."""
    policy = _policy(extra)

    def allowed(name: str) -> bool:
        return policy is None or policy.can_run(user_id, name)

    entries = []
    for cmd in _gateway_available():
        if not allowed(cmd.name):
            continue
        # Like /help, drop internal underscore spellings such as reload_mcp.
        aliases = [a for a in cmd.aliases
                   if not (a.replace("-", "_") == cmd.name.replace("-", "_") and a != cmd.name)]
        entries.append({"name": cmd.name, "description": cmd.describe(), "aliases": aliases,
                        "args_hint": cmd.args_hint, "subcommands": SUBCOMMANDS.get(f"/{cmd.name}", [])})
    for name, meta in _plugin_commands().items():
        if allowed(name) and resolve_command(name) is None:
            entries.append({"name": name, "description": str(meta.get("description") or ""),
                            "aliases": [], "args_hint": str(meta.get("args_hint") or "").strip(),
                            "subcommands": []})
    return entries
