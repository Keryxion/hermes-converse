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


def _hermes_pause_notice(reason="unknown_execution", platform="converse"):
    """Exercise Hermes's producer, so wording/command changes break this contract test."""
    from types import SimpleNamespace
    from gateway.session_ingress import pause_notice

    live = SimpleNamespace(
        pause_notified=False,
        source=SimpleNamespace(platform=SimpleNamespace(value=platform)),
    )
    authority = SimpleNamespace(sessions={"session": live})
    return pause_notice(authority, SimpleNamespace(session_id="session"), reason)


@pytest.mark.parametrize("platform, discard", [
    ("converse", "/discard"),
    ("slack", "/hermes discard"),
])
def test_real_hermes_recovery_commands_survive_real_core_cleaner(platform, discard):
    from hermes_converse import speech

    notice = _hermes_pause_notice(platform=platform)
    # Both the notice and cleaner come from installed Hermes, never a copied fixture or fake.
    assert f"Send {discard} to drop" in notice
    assert "or /reset to start a fresh conversation" in notice
    cleaned = speech.strip_markdown(notice)
    assert f"Send {discard} to drop" in cleaned
    assert "or /reset to start a fresh conversation" in cleaned
    spoken = speech.speakable(cleaned)
    assert f"Send slash {discard[1:]} to drop" in spoken
    assert "or slash reset to start a fresh conversation" in spoken
    assert "it is not re-run" in cleaned


@pytest.mark.parametrize("reason", ["runtime_draining", "session_busy"])
def test_real_hermes_transient_notice_offers_no_recovery_commands(reason):
    from hermes_converse import speech

    notice = _hermes_pause_notice(reason)
    cleaned = speech.strip_markdown(notice)
    assert "message is saved" in cleaned
    assert "/discard" not in cleaned
    assert "/reset" not in cleaned
    assert "slash " not in speech.speakable(cleaned)


def test_real_hermes_reauthorization_failure_only_offers_reset():
    from hermes_converse import speech

    notice = _hermes_pause_notice("policy_changed")
    cleaned = speech.strip_markdown(notice)
    assert "Send /reset to start a fresh conversation" in cleaned
    assert "/discard" not in cleaned
    assert "slash reset" in speech.speakable(cleaned)


@pytest.mark.parametrize("text", [
    "open /usr/bin/env", "see https://example.com/a", "yes and/or no", "100/200",
])
def test_real_cleaner_preserves_existing_noncommand_behavior(text):
    from tools.tts_text_normalize import _strip_markdown_for_tts
    from hermes_converse import speech

    assert speech.strip_markdown(text) == speech._drop_empty_brackets(_strip_markdown_for_tts(text))


def test_slash_commands_are_voiced_but_paths_and_urls_are_not():
    from hermes_converse.speech import speakable

    assert speakable("Send /discard to drop it, or /reset to start fresh.") == (
        "Send slash discard to drop it, or slash reset to start fresh.")
    for text in ("see https://example.com/a", "open /usr/bin/env", "yes and/or no", "100/200"):
        assert speakable(text) == text


@pytest.mark.parametrize("text", [
    "Literal SLASHCOMMAND0X",
    "Literal SLASHCOMMAND0X and SLASHCOMMANDX0X; send /discard or /reset.",
])
def test_command_placeholders_do_not_collide_with_reply_text(text):
    from hermes_converse import speech

    assert speech.strip_markdown(text) == text


def test_recovery_notice_keeps_commands_in_live_and_replay_transcripts():
    from hermes_converse.catchup import replay_frames
    from tests.test_loop import _FakeConverseSession, _Ids, _run_driver, _spoken_bytes

    # Only the notice producer, cleaner and speech driver run: no live session,
    # recovery command, action replay or unknown-turn resolution is involved.
    notice = _hermes_pause_notice()
    sent = _run_driver(_FakeConverseSession(["hello"]), [], [notice],
                       transcripts=True, message_ids=_Ids())
    live = [f["text"] for f in sent if isinstance(f, dict)
            and f.get("type") == "transcript" and f.get("role") == "assistant"]
    assert any("/discard" in text and "/reset" in text for text in live)
    spoken = _spoken_bytes(sent).decode()
    assert "slash discard" in spoken and "slash reset" in spoken
    assert "it is not re-run" in spoken
    frames = replay_frames([{"id": 1, "role": "assistant", "content": notice,
                             "tool_calls": None, "platform_message_id": None}], None)
    assert len(frames) == 1 and frames[0]["replay"]
    assert "/discard" in frames[0]["text"] and "/reset" in frames[0]["text"]


def test_encoded_literal_is_not_mistaken_for_a_command_placeholder():
    from hermes_converse import speech

    assert speech.strip_markdown("Literal SLASHCOMMAND&#48;X; send /discard.") == (
        "Literal SLASHCOMMAND0X; send /discard.")
