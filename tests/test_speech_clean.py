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
