"""The command catalog a device reads with a ``commands`` frame comes from Hermes's own registry."""

from __future__ import annotations

import pytest

pytest.importorskip("hermes_cli.commands")

from hermes_converse.command_catalog import canonical, catalog  # noqa: E402


def test_catalog_lists_gateway_commands_with_their_hints():
    entries = {entry["name"]: entry for entry in catalog({}, "device-7")}
    assert "reset" in entries["new"]["aliases"]
    assert entries["new"]["args_hint"] == "[name]"
    assert "compress" in entries and "help" in entries
    assert all(set(entry) == {"name", "description", "aliases", "args_hint", "subcommands"}
               for entry in entries.values())


def test_catalog_leaves_out_terminal_only_commands():
    names = {entry["name"] for entry in catalog({}, "device-7")}
    assert canonical("quit") is None and "quit" not in names


def test_catalog_follows_the_platform_slash_access_policy():
    extra = {"allow_admin_from": ["owner"], "user_allowed_commands": ["new"]}
    assert {entry["name"] for entry in catalog(extra, "device-7")} == {"help", "whoami", "new"}
    assert len(catalog(extra, "owner")) == len(catalog({}, "owner"))


def test_canonical_resolves_aliases_and_rejects_unknown_names():
    assert canonical("reset") == "new"
    assert canonical("new") == "new"
    assert canonical("no-such-command") is None
