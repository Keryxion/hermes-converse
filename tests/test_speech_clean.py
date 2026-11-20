"""Speech text cleaning that the plugin adds on top of Hermes's TTS cleaner."""
import pytest

from hermes_converse.speech import _drop_empty_brackets


@pytest.mark.parametrize("text, expected", [
    ("Done — PR 39 merged ( ).", "Done — PR 39 merged."),
    ("See the docs [] for more.", "See the docs for more."),
    ("Nothing to change (really).", "Nothing to change (really)."),
    ("Two ( ) of them () here.", "Two of them here."),
])
def test_brackets_left_empty_by_a_removed_link_are_dropped(text, expected):
    assert _drop_empty_brackets(text) == expected


NOTICE = ("⏸️ This conversation is paused: a previous turn did not finish when Hermes restarted, "
          "and only you know whether to send it again. Your message is saved. Send /discard to drop "
          "the lost turn and continue (it is not re-run), or /reset to start a fresh conversation.")


def test_slash_commands_survive_the_core_cleaner(monkeypatch):
    """Hermes's cleaner reads a bare "/discard" as a path and drops it (#191)."""
    import re
    import sys
    import types

    from hermes_converse import speech

    core = types.ModuleType("tools.tts_text_normalize")
    core._strip_markdown_for_tts = lambda text: re.sub(r"(?<!\S)/\S+\s?", "", text)
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.tts_text_normalize", core)

    cleaned = speech.strip_markdown(NOTICE)
    assert "Send /discard to drop" in cleaned
    assert "or /reset to start a fresh conversation" in cleaned
    assert speech.strip_markdown("Slack: /hermes discard") == "Slack: /hermes discard"


def test_slash_commands_are_voiced_but_paths_and_urls_are_not():
    from hermes_converse.speech import speakable

    assert speakable("Send /discard to drop it, or /reset to start fresh.") == (
        "Send slash discard to drop it, or slash reset to start fresh.")
    for text in ("see https://example.com/a", "open /usr/bin/env", "yes and/or no", "100/200"):
        assert speakable(text) == text
