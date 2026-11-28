"""Agent tools that control the voice device whose conversation calls them."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from typing import Any, Callable

TOOLSET = "converse"
TONES = ("confirm", "error")
TONE_TTL = 30  # seconds a tone may wait behind queued speech before it is stale

SCHEMAS = {
    "client_diagnostics": {
        "name": "client_diagnostics",
        "description": (
            "Read fresh diagnostics from this conversation's voice client: version, build, microphone, "
            "wake detection, playback, etc"
            "Report errors as they are; do not substitute server logs."),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "command": {
        "name": "command",
        "description": (
            "Voice devices only. Run a gateway command for this conversation (catalog names, aliases "
            "and plugin commands). The person approves every call. Returns the gateway reply for you to "
            "summarize. If denied, say it was not run and stop unless they ask again. "
            "Start over: name new, args []; it applies after your reply has played."),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Command name without a slash, such as new."},
            "args": {"type": "array", "items": {"type": "string"},
                     "description": "Command arguments, such as [here, 3] for compress; default []."}},
            "required": ["name"]},
    },
    "set_wake_word": {
        "name": "set_wake_word",
        "description": (
            "Voice devices only. Change this device's wake word. With no phrase, list the available "
            "phrases and the current one. With a listed phrase, switch: it is done only when the result "
            "is ok; otherwise the old wake word stays, so say it did not change."),
        "parameters": {"type": "object", "properties": {
            "phrase": {"type": "string", "description": "A listed phrase, such as Hey Arya."}}},
    },
    "controls": {
        "name": "controls",
        "description": ("Voice devices only. Read or immediately set this device's microphone mute and playback volume. "
                        "Muted devices cannot hear voice requests to unmute. Results are device reports, not guarantees; "
                        "if reported is false or a value differs, do not claim the change succeeded."),
        "parameters": {"type": "object", "properties": {
            "muted": {"type": "boolean", "description": "Mute or unmute the microphone immediately."},
            "volume": {"type": "number", "minimum": 0, "maximum": 1,
                       "description": "Playback volume from 0 (silent) to 1 (maximum)."}},
            "additionalProperties": False},
    },
    "announce": {
        "name": "announce",
        "description": (
            "Speak a message, after a chime, on every other connected voice device (\"announce dinner's "
            "ready\"); not on this one. The result counts devices reached; 0 means nobody heard it."),
        "parameters": {"type": "object", "properties": {
            "message": {"type": "string", "description": "What to say, worded for the listeners."}},
            "required": ["message"]},
    },
    "speak_this": {
        "name": "speak_this",
        "description": (
            "Voice devices only. Say exactly this text. From the first call in a turn, only text given "
            "to this tool is spoken. An empty string says nothing."),
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                       "required": ["text"]},
    },
    "play_tone": {
        "name": "play_tone",
        "description": "Voice devices only. Play a tone: confirm (done) or error (could not do it).",
        "parameters": {"type": "object", "properties": {"tone": {"type": "string", "enum": list(TONES)}},
                       "required": ["tone"]},
    },
    "standby": {
        "name": "standby",
        "description": ("Voice devices only. Return the device to its wake word after this turn's speech. "
                        "Rarely needed: it stops listening by itself after a few seconds of silence. Call it "
                        "only when the person says they are done (\"that's all\", \"good night\"), "
                        "never just because a request is finished: they may add to it."),
        "parameters": {"type": "object", "properties": {}},
    },
    "no_barge_in": {
        "name": "no_barge_in",
        "description": ("Voice devices only. For this turn only, sound at the microphone does not interrupt "
                        "you; the wake word or a device control does. Call it only when the person says you "
                        "keep cutting yourself off, or your replies keep being cut and what arrives next is "
                        "your own reply or nothing meant for you. Otherwise leave it."),
        "parameters": {"type": "object", "properties": {}},
    },
}


def _result(value: str) -> str:
    return json.dumps({"result": value})


def play_tone_on(hub: Any, conversation_id: str, tone: str) -> None:
    """Play *tone* in the conversation's reply audio: at this point of the running turn's reply,
    else after whatever the device has queued to say."""
    from .loop import Tone

    adapter = hub.gateway
    turn = adapter._turns.get(conversation_id) if adapter is not None else None
    if turn is not None and turn.started and turn.outcome is None and not turn.stopped and not turn.detached:
        turn.on_delta(Tone(tone))
    else:
        hub.publish(conversation_id, "", f"tone:{uuid.uuid4().hex}", expires_at=time.time() + TONE_TTL,
                    transient=True, tones=(tone,))


def make_handlers(hub: Any) -> dict[str, Callable[..., Any]]:
    def device() -> tuple[str, Any]:
        """("", attachment) for a voice device conversation, else (error result, None)."""
        from gateway.session_context import get_session_env

        if get_session_env("HERMES_SESSION_PLATFORM", "") != "converse":
            return "not_a_device", None
        attachment = hub.attachment(get_session_env("HERMES_SESSION_CHAT_ID", ""))
        if attachment is None:
            return "not_attached", None
        return "", attachment

    def turn_of(attachment: Any) -> Any:
        adapter = hub.gateway
        return adapter._turns.get(attachment.conversation_id) if adapter is not None else None

    def server_turn_of(attachment: Any) -> Any:
        adapter = hub.gateway
        return adapter._server_turns.get(attachment.conversation_id) if adapter is not None else None

    def speak_this(args: dict, **kw: Any) -> str:
        error, attachment = device()
        if attachment is None:
            return _result(error)
        text = args.get("text")
        turn = turn_of(attachment)
        if turn is None or not turn.started or turn.outcome is not None:
            server_turn = server_turn_of(attachment)
            if server_turn is not None:
                server_turn.speak(text if isinstance(text, str) else "")
            return _result("ok")
        turn.exclusive = True
        if isinstance(text, str) and text.strip() and not turn.stopped:
            turn.on_delta("\n" + text + "\n")
        return _result("ok")

    def announce(args: dict, **kw: Any) -> str:
        from gateway.session_context import get_session_env

        message = args.get("message")
        if not isinstance(message, str) or not message.strip():
            return json.dumps({"error": "message must be the text to announce"})
        # Every attached device but the one whose conversation asked, when a device did.
        caller = (get_session_env("HERMES_SESSION_CHAT_ID", "")
                  if get_session_env("HERMES_SESSION_PLATFORM", "") == "converse" else "")
        return json.dumps({"result": "ok", "devices": hub.announce_all(message.strip(), but=caller)})

    def play_tone(args: dict, **kw: Any) -> str:
        error, attachment = device()
        if attachment is None:
            return _result(error)
        tone = args.get("tone")
        if tone not in TONES:
            return json.dumps({"error": "tone must be confirm or error"})
        play_tone_on(hub, attachment.conversation_id, tone)
        return _result("ok")

    def standby(args: dict, **kw: Any) -> str:
        error, attachment = device()
        if attachment is None:
            return _result(error)
        server_turn = server_turn_of(attachment)
        turn = server_turn or turn_of(attachment)
        if turn is not None:
            turn.standby, turn.mute = True, False
        if server_turn is None:
            attachment.session.unmute()
        return _result("ok")

    def no_barge_in(args: dict, **kw: Any) -> str:
        error, attachment = device()
        if attachment is None:
            return _result(error)
        server_turn = server_turn_of(attachment)
        if server_turn is not None:
            server_turn.no_barge_in = True
        else:
            attachment.session.ignore_microphone(muted=False)
        return _result("ok")

    async def controls(args: dict, **kw: Any) -> str:
        from .admin import _is_level

        error, attachment = device()
        if attachment is None or attachment.controls is None:
            return _result(error or "not_attached")
        if set(args) - {"muted", "volume"}:
            return json.dumps({"error": "unknown controls field"})
        if "muted" in args and not isinstance(args["muted"], bool):
            return json.dumps({"error": "muted must be a boolean"})
        if "volume" in args and not _is_level(args["volume"]):
            return json.dumps({"error": "volume must be a number from 0 to 1"})
        result = await asyncio.wrap_future(attachment.controls(args))
        return json.dumps(result)

    async def command(args: dict, **kw: Any) -> str:
        error, attachment = device()
        if attachment is None or attachment.command is None:
            return _result(error or "not_attached")
        name, argv = args.get("name"), args.get("args", [])
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_-]{1,40}", name)
                or not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv)):
            return _result("bad_command")
        from gateway.platforms.base_exec_approval import approval_timeout_seconds

        result = await asyncio.wrap_future(attachment.command(name, list(argv), approval_timeout_seconds()))
        if result == "denied":
            return json.dumps({"result": "denied", "message": (
                "The person did not approve this call. Acknowledge that it was not run and stop. "
                "Only ask again if the person explicitly renews the request.")})
        return _result(result)

    async def client_diagnostics(args: dict, **kw: Any) -> str:
        error, attachment = device()
        if attachment is None or attachment.diagnostics is None:
            return json.dumps({"error": error or "not_attached"})
        result = await asyncio.wrap_future(attachment.diagnostics())
        return json.dumps(result, ensure_ascii=False)

    async def set_wake_word(args: dict, **kw: Any) -> str:
        error, attachment = device()
        if attachment is None:
            return _result(error or "not_attached")
        if attachment.wake_word is None:  # the device sent no wake_word_format
            return json.dumps({"result": "failed", "error": "model_unavailable"})
        phrase = args.get("phrase")
        if phrase is not None and (not isinstance(phrase, str) or not phrase.strip()):
            return json.dumps({"error": "phrase must be a listed wake phrase"})
        result = await asyncio.wrap_future(attachment.wake_word(phrase.strip() if phrase else None))
        return json.dumps(result, ensure_ascii=False)

    return {"announce": announce, "speak_this": speak_this, "play_tone": play_tone, "standby": standby,
            "no_barge_in": no_barge_in, "command": command, "client_diagnostics": client_diagnostics,
            "set_wake_word": set_wake_word, "controls": controls}


def register_tools(ctx: Any, hub: Any) -> None:
    for name, handler in make_handlers(hub).items():
        ctx.register_tool(name=name, toolset=TOOLSET, schema=SCHEMAS[name], handler=handler,
                          description=SCHEMAS[name]["description"], emoji="🎙️",
                          is_async=asyncio.iscoroutinefunction(handler))

    # Hermes turns an empty model response into a warning before the adapter sees it. Keep the
    # tool's explicit silence with the exact agent turn, even for server-started turns that have
    # no adapter processing bracket. Never silence an actual error or a different turn.
    silent_turns: dict[tuple[str, str], bool] = {}
    lock = threading.Lock()

    def post_tool_call(*, tool_name=None, args=None, result=None, session_id=None, turn_id=None, **kw):
        if tool_name != "speak_this" or not session_id or not turn_id or not isinstance(args, dict):
            return
        try:
            value = json.loads(result) if isinstance(result, str) else result
        except (ValueError, TypeError):
            return
        if not isinstance(value, dict) or value.get("result") != "ok" or not isinstance(args.get("text"), str):
            return
        key = (session_id, turn_id)
        with lock:
            silent_turns[key] = silent_turns.get(key, True) and not args["text"].strip()

    def transform_llm_output(*, response_text="", session_id=None, turn_id=None, platform=None, **kw):
        if platform != "converse" or not session_id or not turn_id:
            return None
        with lock:
            silent = silent_turns.pop((session_id, turn_id), False)
        if silent and response_text == "(empty)":
            return "[SILENT]"
        return None

    def on_session_end(*, session_id=None, turn_id=None, **kw):
        with lock:
            silent_turns.pop((session_id, turn_id), None)

    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("transform_llm_output", transform_llm_output)
    ctx.register_hook("on_session_end", on_session_end)
