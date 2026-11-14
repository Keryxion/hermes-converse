import json

from hermes_converse.catchup import replay_frames


def row(id, role, content=None, **extra):
    return {"id": id, "role": role, "content": content, "tool_calls": None, "platform_message_id": None,
            "display_kind": None, **extra}


def speak(text):
    return [{"id": "c", "type": "function", "function": {"name": "speak_this",
                                                          "arguments": json.dumps({"text": text})}}]


ROWS = [
    row(1, "user", "What is on today?", platform_message_id="t1"),
    row(2, "assistant", "Two things **today**."),
    row(3, "user", "Remind me", platform_message_id="t2"),
    row(4, "assistant", None, tool_calls=speak("Done, Sam.")),
    row(5, "tool", "ok"),
    row(6, "assistant", "internal wrap-up"),
    row(7, "user", "Anything else?", platform_message_id="t3"),
    row(8, "assistant", "[SILENT]"),
]


def test_replay_is_user_and_assistant_text_for_speech():
    frames = replay_frames(ROWS, None)
    assert [(f["message_id"], f["role"], f["text"]) for f in frames] == [
        ("1", "user", "What is on today?"), ("2", "assistant", "Two things today."),
        ("3", "user", "Remind me"), ("6", "assistant", "Done, Sam."), ("7", "user", "Anything else?")]
    assert all(f["replay"] and f["type"] == "transcript" for f in frames)
    assert frames[1]["turn_id"] == "t1"


def test_after_replays_only_what_follows_and_unknown_after_gets_recent():
    assert [f["message_id"] for f in replay_frames(ROWS, "2")] == ["3", "6", "7"]
    assert replay_frames(ROWS, "7") == []
    assert len(replay_frames(ROWS, "999")) == 5


def test_recent_limits():
    rows = [row(i, "user", f"m{i}") for i in range(1, 201)]
    assert len(replay_frames(rows, None)) == 20
    assert len(replay_frames(rows, "1")) == 100


def test_an_interrupted_turns_notice_is_not_replayed():
    rows = [row(1, "user", "Put that on the PR."),
            row(2, "assistant", "Operation interrupted: waiting for model response (10.6s elapsed).")]
    assert [(f["message_id"], f["role"]) for f in replay_frames(rows, None)] == [("1", "user")]
