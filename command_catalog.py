"""The gateway commands a device may name in a ``command`` frame, listed the way Hermes lists them
for Telegram's command menu.

Like that menu, the list is the same for every device: the gateway checks who may run a command
when it runs. Arguments have no schema in Hermes: ``args_hint`` (such as ``[name]``) and
``subcommands`` are all it knows, so ``args`` stays free text the command parses.
"""

from __future__ import annotations

from typing import Any, Optional

from hermes_cli.commands import SUBCOMMANDS, _iter_plugin_command_entries, resolve_command
from hermes_cli.commands_platforms import _gateway_available_commands


def canonical(name: str) -> Optional[str]:
    """The canonical name of a command or alias the gateway runs; None when there is no such command."""
    cmd = resolve_command(name)
    if cmd is not None:
        return cmd.name if cmd in _gateway_available_commands() else None
    return name if any(plugin == name for plugin, _d, _h in _iter_plugin_command_entries()) else None


def description(name: str) -> str:
    """What canonical command *name* does, as the catalog lists it."""
    cmd = resolve_command(name)
    if cmd is not None:
        return cmd.describe()
    return next((d for plugin, d, _h in _iter_plugin_command_entries() if plugin == name), "")


def catalog() -> list[dict[str, Any]]:
    """Every command a chat runs, in registry order, then plugin commands."""
    entries = []
    for cmd in _gateway_available_commands():
        # Like /help, drop internal underscore spellings such as reload_mcp.
        aliases = [a for a in cmd.aliases
                   if not (a.replace("-", "_") == cmd.name.replace("-", "_") and a != cmd.name)]
        entries.append({"name": cmd.name, "description": cmd.describe(), "aliases": aliases,
                        "args_hint": cmd.args_hint, "subcommands": SUBCOMMANDS.get(f"/{cmd.name}", [])})
    for name, description, args_hint in _iter_plugin_command_entries():
        if resolve_command(name) is None:
            entries.append({"name": name, "description": description, "aliases": [],
                            "args_hint": args_hint, "subcommands": []})
    return entries
