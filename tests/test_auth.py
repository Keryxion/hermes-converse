"""Unit tests for the scoped voice-token credential (issue #11)."""

from __future__ import annotations

from hermes_converse.auth import (
    offered_key_protocol,
    offered_voice_token_protocol,
    verify_voice_token,
)
from hermes_converse.settings import DEFAULT_VOICE_TOKEN, load_settings


def test_offered_voice_token_protocol_single():
    assert offered_voice_token_protocol("hermes-voice-v1, hermes-voice-token.secret123") == "secret123"


def test_offered_voice_token_protocol_absent():
    assert offered_voice_token_protocol("hermes-voice-v1, hermes-key.xyz") is None
    assert offered_voice_token_protocol("") is None


def test_offered_voice_token_protocol_ambiguous():
    assert offered_voice_token_protocol("hermes-voice-token.a, hermes-voice-token.b") == ""


def test_offered_key_protocol_still_works():
    assert offered_key_protocol("hermes-voice-v1, hermes-key.ABC") == "ABC"
    assert offered_key_protocol("hermes-voice-v1, hermes-voice-token.ABC") is None


def test_verify_voice_token_match():
    assert verify_voice_token("tok", "tok") is True


def test_verify_voice_token_mismatch():
    assert verify_voice_token("tok", "other") is False


def test_verify_voice_token_empty_never_authenticates():
    assert verify_voice_token("", "tok") is False
    assert verify_voice_token("tok", "") is False
    assert verify_voice_token("", "") is False


def test_load_settings_voice_token():
    assert DEFAULT_VOICE_TOKEN == ""
    s = load_settings(lambda k, d=None: {"voice_token": "  padded  "}.get(k, d))
    assert s.voice_token == "padded"
    s2 = load_settings(lambda k, d=None: d)
    assert s2.voice_token == ""
