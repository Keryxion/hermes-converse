"""Settings."""

import pytest

from hermes_converse.settings import load_settings


class TestSettings:
    def test_defaults_without_any_config(self):
        s = load_settings(lambda key, default=None: default)
        assert s.model is None and s.provider is None
        assert s.turn_timeout == 120.0 and s.tail_fallback == 45.0 and s.max_tts_chars == 1500
        assert s.smart_turn_enabled is True and s.endpoint_threshold == 0.5
        assert s.endpoint_silence_ms == 700 and s.endpoint_max_silence_ms == 3000
        assert s.approval_choice == "deny" and s.api_base_url == ""

    def test_endpoint_silence_windows(self):
        def load(cfg):
            return load_settings(lambda key, default=None: cfg.get(key, default))
        s = load({"endpoint_silence_ms": "900", "endpoint_max_silence_ms": 2500})
        assert s.endpoint_silence_ms == 900 and s.endpoint_max_silence_ms == 2500
        s = load({"endpoint_silence_ms": 1200, "endpoint_max_silence_ms": 500})
        assert s.endpoint_max_silence_ms == 1200  # never shorter than the candidate pause
        s = load({"endpoint_silence_ms": -1, "endpoint_max_silence_ms": "nan"})
        assert s.endpoint_silence_ms == 700 and s.endpoint_max_silence_ms == 3000

    def test_values_are_coerced_and_clamped(self):
        cfg = {"voice_model": "gpt-x", "voice_provider": "", "turn_timeout_seconds": "30",
               "playback_tail_timeout_seconds": "not a number", "max_tts_chars_per_turn": 200.7,
               "smart_turn_enabled": True,
               "endpoint_threshold": 5, "approval_choice": "ONCE", "api_base_url": "http://127.0.0.1:9000/"}
        s = load_settings(lambda key, default=None: cfg.get(key, default))
        assert s.model == "gpt-x" and s.provider is None
        assert s.turn_timeout == 30.0 and s.tail_fallback == 45.0 and s.max_tts_chars == 200
        assert s.smart_turn_enabled is True and s.endpoint_threshold == 1.0
        assert s.approval_choice == "once" and s.api_base_url == "http://127.0.0.1:9000"

    @pytest.mark.parametrize("enabled", [True, False])
    def test_smart_turn_boolean(self, enabled):
        assert load_settings({"smart_turn_enabled": enabled}.get).smart_turn_enabled is enabled

    @pytest.mark.parametrize("invalid", ["true", "false", "smart-turn-v3", "typo", "", None, 0, 1])
    def test_smart_turn_rejects_non_boolean_values(self, invalid):
        with pytest.raises(ValueError, match="smart_turn_enabled must be a boolean"):
            load_settings({"smart_turn_enabled": invalid}.get)

    def test_bad_approval_choice(self):
        assert load_settings(lambda k, d=None: "always" if k == "approval_choice" else d).approval_choice == "deny"
