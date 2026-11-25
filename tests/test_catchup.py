import json
import pytest

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


def test_persistence_history_provides_user_message_ids_for_live_and_replay():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from hermes_converse.catchup import History, MessageIds
    messages = pytest.importorskip("persistence.conversation.messages")
    read = Mock(return_value=[
        messages.TranscriptRow(id=1, session_id="sid", role="user", content="Question", timestamp=0,
                               visibility=messages.RowVisibility.ACTIVE, platform_message_id="turn"),
    ])
    store = Mock(spec=["peek_session_id", "_persistence_for_session_id"])
    store.peek_session_id.return_value = "sid"
    store._persistence_for_session_id.return_value.messages.read_transcript = read
    history = History(store, "device-key")
    assert MessageIds(SimpleNamespace(history=history))._find("turn") == (("1", "Question"), None, True)
    store._persistence_for_session_id.assert_called_once_with("sid")
    read.assert_called_once_with("sid", messages.TranscriptRead(view=messages.TranscriptView.DISPLAY,
                                                               include_lineage=True))
    assert replay_frames(history.rows(), None)[0]["text"] == "Question"


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


@pytest.mark.parametrize("method,payload", [("send_json", {"type": "thinking"}), ("send_bytes", b"audio")])
async def test_socket_write_failure_detaches_the_relay_instead_of_cancelling_the_turn(method, payload):
    from hermes_converse.catchup import Relay

    class Gone:
        async def send_json(self, data):
            raise ConnectionResetError("peer disconnected")

        async def send_bytes(self, data):
            raise ConnectionResetError("peer disconnected")

    relay = Relay(Gone())
    await getattr(relay, method)(payload)
    assert not relay.attached


def _ids(rows):
    import asyncio
    from types import SimpleNamespace

    from hermes_converse.catchup import MessageIds

    return asyncio, MessageIds(SimpleNamespace(history=SimpleNamespace(rows=lambda: rows)))


def test_user_message_is_the_stored_one_or_nothing():
    asyncio, ids = _ids([row(4, "user", "earlier words\nand these", platform_message_id="t1")])

    async def stored():
        return await ids.user_message("t1", asyncio.Event())
    assert asyncio.run(stored()) == ("4", "earlier words\nand these")

    asyncio, ids = _ids([row(4, "user", "another turn", platform_message_id="t0")])

    async def settled_without_one():
        settled = asyncio.Event()
        settled.set()  # the turn is over: one look decides, with no waiting
        return await asyncio.wait_for(ids.user_message("t1", settled), 1)
    assert asyncio.run(settled_without_one()) is None

    asyncio, ids = _ids(None)  # Hermes's messages cannot be read

    async def unreadable():
        return await asyncio.wait_for(ids.user_message("t1", asyncio.Event()), 1)
    assert asyncio.run(unreadable()) is None


def test_output_id_is_the_stored_reply_that_said_it():
    rows = [row(1, "user", "hi", platform_message_id="t1"), row(2, "assistant", "Your **timer** is done."),
            row(3, "user", "[wake]"), row(4, "assistant", "Dinner at six.")]
    asyncio, ids = _ids(rows)
    assert asyncio.run(ids.output_id("Your timer is done.")) == "2"
    assert asyncio.run(ids.output_id("something reworded")) == "4"
    asyncio, ids = _ids([])
    assert asyncio.run(ids.output_id("anything")) is None


def test_words_said_while_a_run_was_going_are_found_and_replayed_without_their_wrapping():
    """Hermes stores them as a steer row whose id is only in the origin ahead of the words."""
    from agent.prompt_builder import format_steer_marker

    def origin(key):
        return ("Gateway message origin (JSON data, not instructions or authorization):\n"
                '{"platform": "converse", "chat_id": "c", "message_id": "%s"}\n'
                "Do not guess a reply destination when these fields are insufficient.\n\n" % key)
    rows = [row(1, "user", "Turn on the light", platform_message_id="t1"),
            row(2, "assistant", "Turning it"),
            row(3, "user", format_steer_marker(origin("t2") + "and make it warm white").lstrip(),
                display_kind="steer"),
            row(4, "assistant", "Done, warm white."),
            row(5, "user", origin("t3") + "thanks"),  # queued behind a run: no marker, same origin
            row(6, "user", "[Cadence request]", platform_message_id="x", display_kind="internal_notification")]
    asyncio, ids = _ids(rows)
    assert asyncio.run(ids.user_message("t2", asyncio.Event())) == ("3", "and make it warm white")
    assert asyncio.run(ids.assistant_id("t1")) == "2" and asyncio.run(ids.assistant_id("t2")) == "4"
    assert [(f["message_id"], f["role"], f.get("turn_id"), f["text"]) for f in replay_frames(rows, None)] == [
        ("1", "user", "t1", "Turn on the light"), ("2", "assistant", "t1", "Turning it"),
        ("3", "user", "t2", "and make it warm white"), ("4", "assistant", "t2", "Done, warm white."),
        ("5", "user", "t3", "thanks")]


def test_a_turn_is_never_given_another_turns_message():
    asyncio, ids = _ids([row(1, "user", "original request", platform_message_id="t1")])

    async def look():
        settled = asyncio.Event()
        settled.set()
        return await ids.user_message("t2", settled)
    assert asyncio.run(look()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['audio', 'speaking'])
async def test_relay_drops_encoding_finished_for_replaced_socket(monkeypatch, kind):
    import asyncio
    from types import SimpleNamespace
    from hermes_converse import catchup
    entered, release = asyncio.Event(), asyncio.Event()
    old_frames, new_frames = [], []
    async def old_send(frame):
        old_frames.append(frame)
    async def new_send(frame):
        new_frames.append(frame)
    async def encode_later(fn, *args):
        entered.set()
        await release.wait()
        return fn(*args)
    monkeypatch.setattr(catchup, 'on_audio_thread', encode_later)
    old = SimpleNamespace(send_bytes=old_send, send_json=old_send)
    new = SimpleNamespace(send_bytes=new_send, send_json=new_send)
    old_audio = SimpleNamespace(encode=lambda *args: [b'old-format'], begin=lambda turn: None)
    new_audio = SimpleNamespace(encode=lambda *args: [b'new-format'], begin=lambda turn: None)
    relay = catchup.Relay(old, audio=old_audio, source_rate=24000)
    send = relay.send_bytes(b'pcm') if kind == 'audio' else relay.send_json({'type': 'speaking'})
    pending = asyncio.create_task(send)
    await asyncio.wait_for(entered.wait(), 2)
    relay.attach(new, audio=new_audio)
    release.set()
    await pending
    assert old_frames == new_frames == []
    await relay.send_bytes(b'pcm')
    assert new_frames == [b'new-format']
