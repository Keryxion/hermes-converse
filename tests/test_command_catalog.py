"""The command catalog a device reads with a ``commands`` frame comes from Hermes's own registry."""

from __future__ import annotations

import pytest

pytest.importorskip("hermes_cli.commands")

from hermes_converse.command_catalog import canonical, catalog  # noqa: E402


def test_catalog_lists_gateway_commands_with_their_hints():
    entries = {entry["name"]: entry for entry in catalog()}
    assert "reset" in entries["new"]["aliases"]
    assert entries["new"]["args_hint"] == "[name]"
    assert "compress" in entries and "help" in entries
    assert all(set(entry) == {"name", "description", "aliases", "args_hint", "subcommands"}
               for entry in entries.values())


def test_catalog_leaves_out_terminal_only_commands():
    names = {entry["name"] for entry in catalog()}
    assert canonical("quit") is None and "quit" not in names


def test_catalog_is_the_same_whatever_the_access_settings():
    # As with Telegram's menu, the gateway checks who may run a command when it runs, not here.
    assert {entry["name"] for entry in catalog()} >= {"new", "title", "help", "whoami"}


def test_canonical_resolves_aliases_and_rejects_unknown_names():
    assert canonical("reset") == "new"
    assert canonical("new") == "new"
    assert canonical("no-such-command") is None
