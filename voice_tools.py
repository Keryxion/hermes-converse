"""Agent tools that control the voice device whose conversation calls them."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

TOOLSET = "converse"
TONES = ("confirm", "error")

SCHEMAS = {
    "speak_this": {
        "name": "speak_this",
        "description": (
            "Voice devices only. Say exactly this text aloud. From the first call in a turn, "
            "nothing else you write in that turn is spoken, only text given to this tool. "
            "Call with an empty string to say nothing."),
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                       "required": ["text"]},
    },
    "play_tone": {
        "name": "play_tone",
        "description": "Voice devices only. Play a short tone: confirm (done) or error (could not do it).",
        "parameters": {"type": "object", "properties": {"tone": {"type": "string", "enum": list(TONES)}},
                       "required": ["tone"]},
    },
    "standby": {
        "name": "standby",
        "description": ("Voice devices only. Sends the device back to waiting for its wake word "
                        "once this turn's speech has played. You rarely need it: when the person says "
                        "nothing more, the device stops listening by itself after a few seconds. Call it "
                        "only when the person has said they are done (\"that's all\", \"thanks, bye\", "
                        "\"good night\"). Finishing what they asked is not a sign they are done: after "
                        "\"turn on the lights\", do not call it, since they may add \"and the kitchen "
                        "too\"."),
        "parameters": {"type": "object", "properties": {}},
    },
    "command": {
        "name": "command",
        "description": ("Voice devices only. Runs a gateway slash command for this conversation, once the "
                        "person approves it out loud, and returns its reply (not spoken). For example "
                        "\"start over\" -> /new, \"compress this\" -> /compress. /new takes effect when "
                        "this turn's reply has played."),
        "parameters": {"type": "object", "properties": {"text": {
            "type": "string", "description": "The command with its slash and any arguments, such as /new."}},
            "required": ["text"]},
    },
}


def _result(value: str) -> str:
    return json.dumps({"result": value})


def make_handlers(hub: Any) -> dict[str, Callable[..., str]]:
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

    def play_tone(args: dict, **kw: Any) -> str:
        error, attachment = device()
        if attachment is None:
            return _result(error)
        tone = args.get("tone")
        if tone not in TONES:
            return json.dumps({"error": "tone must be confirm or error"})
        if attachment.send is None:
            return _result("not_attached")
        attachment.send({"type": "tone", "tone": tone})
        return _result("ok")

    def standby(args: dict, **kw: Any) -> str:
        error, attachment = device()
        if attachment is None:
            return _result(error)
        server_turn = server_turn_of(attachment)
        if server_turn is not None:
            server_turn.standby = True
        else:
            turn = turn_of(attachment)
            if turn is not None:
                turn.standby = True
        return _result("ok")

    async def command(args: dict, **kw: Any) -> str:
        error, attachment = device()
        if attachment is None or attachment.command is None:
            return _result(error or "not_attached")
        text = args.get("text")
        if not isinstance(text, str) or not text.strip().startswith("/"):
            return _result("unknown_command")
        from gateway.platforms.base_exec_approval import approval_timeout_seconds

        # The socket's loop asks the person and runs the command; this tool call waits for it.
        return _result(await asyncio.wrap_future(attachment.command(text.strip(), approval_timeout_seconds())))

    return {"speak_this": speak_this, "play_tone": play_tone, "standby": standby, "command": command}


def register_tools(ctx: Any, hub: Any) -> None:
    for name, handler in make_handlers(hub).items():
        ctx.register_tool(name=name, toolset=TOOLSET, schema=SCHEMAS[name], handler=handler,
                          description=SCHEMAS[name]["description"], emoji="🎙️",
                          is_async=asyncio.iscoroutinefunction(handler))
