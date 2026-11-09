"""Stop phrases (the user ends the exchange), end phrases (the assistant signs off) and settings."""

import pytest

from hermes_converse.phrases import is_end_phrase, normalize_phrases, strip_end_phrase
from hermes_converse.settings import DEFAULT_END_PHRASES, load_settings


class TestEndPhrase:
    def test_default_is_over_and_out(self):
        assert DEFAULT_END_PHRASES == ("over and out",)

    @pytest.mark.parametrize("reply", [
        "Over and out.", "over and out", "OVER AND OUT!",
        "Your lights are on. Over and out.", "Done — over and out.",
    ])
    def test_reply_ending_with_signoff_matches(self, reply):
        assert is_end_phrase(reply, DEFAULT_END_PHRASES) is True

    @pytest.mark.parametrize("reply", ["It's 72 degrees out.", "Over and out is a phrase pilots use.", "", "   "])
    def test_non_signoff_reply_does_not_match(self, reply):
        assert is_end_phrase(reply, DEFAULT_END_PHRASES) is False

    def test_disabled_when_no_end_phrases(self):
        assert is_end_phrase("Over and out.", ()) is False


class TestStripEndPhrase:
    @pytest.mark.parametrize("reply,spoken", [
        ("Give me a shout. Over and out.", "Give me a shout."),
        ("All done — over and out!", "All done"),
        ("Over and out.", ""),
        ("Talk soon, over and out", "Talk soon"),
    ])
    def test_trailing_signoff_is_removed(self, reply, spoken):
        assert strip_end_phrase(reply, DEFAULT_END_PHRASES) == spoken

    def test_non_trailing_mention_is_untouched(self):
        text = "Over and out is radio slang for goodbye."
        assert strip_end_phrase(text, DEFAULT_END_PHRASES) == text

    def test_plain_reply_untouched(self):
        assert strip_end_phrase("It's sunny and 72.", DEFAULT_END_PHRASES) == "It's sunny and 72."


class TestNormalizePhrases:
    def test_string_list_and_tuple(self):
        assert normalize_phrases("Bye", ("x",)) == ("bye",)
        assert normalize_phrases(["Stop", " Cancel "], ("x",)) == ("stop", "cancel")
        assert normalize_phrases(("a",), ("x",)) == ("a",)

    def test_empty_list_disables_and_malformed_falls_back(self):
        assert normalize_phrases([], ("x",)) == ()
        assert normalize_phrases({"a": 1}, ("x",)) == ("x",)
        assert normalize_phrases(None, ("x",)) == ("x",)


class TestSettings:
    def test_defaults_without_any_config(self):
        s = load_settings(lambda key, default=None: default)
        assert s.name == "Arya" and s.model is None and s.provider is None
        assert s.turn_timeout == 120.0 and s.tail_fallback == 45.0 and s.max_tts_chars == 1500
        assert s.end_phrases == ("over and out",)
        assert s.endpoint_model is False and s.endpoint_threshold == 0.5
        assert s.approval_choice == "deny" and s.api_base_url == ""

    def test_values_are_coerced_and_clamped(self):
        cfg = {"name": "  Hal ", "voice_model": "gpt-x", "voice_provider": "", "turn_timeout_seconds": "30",
               "playback_tail_timeout_seconds": "not a number", "max_tts_chars_per_turn": 200.7,
               "end_phrases": [], "endpoint_model": "smart-turn-v3",
               "endpoint_threshold": 5, "approval_choice": "ONCE", "api_base_url": "http://127.0.0.1:9000/"}
        s = load_settings(lambda key, default=None: cfg.get(key, default))
        assert s.name == "Hal" and s.model == "gpt-x" and s.provider is None
        assert s.turn_timeout == 30.0 and s.tail_fallback == 45.0 and s.max_tts_chars == 200
        assert s.end_phrases == ()
        assert s.endpoint_model is True and s.endpoint_threshold == 1.0
        assert s.approval_choice == "once" and s.api_base_url == "http://127.0.0.1:9000"

    def test_endpoint_model_off_values_and_bad_approval_choice(self):
        for off in ("", "none", "False", "off", "0"):
            assert load_settings(lambda k, d=None, off=off: off if k == "endpoint_model" else d).endpoint_model is False
        assert load_settings(lambda k, d=None: "always" if k == "approval_choice" else d).approval_choice == "deny"
