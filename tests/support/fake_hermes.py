"""A scriptable stand-in for the Hermes the plugin talks to.

Two surfaces, both the real shapes:

* The API server over HTTP, served in-process: ``GET /v1/capabilities`` (the auth probe),
  ``GET /api/model/options`` and ``GET /api/sessions/{id}/messages`` (the live session behind an
  API conversation from before conversations were gateway sessions).
* The gateway runner behind the converse platform: :meth:`FakeHermes.handle` is the message handler
  a real ``BasePlatformAdapter`` hands each admitted event to. It streams the scripted reply through
  the adapter's plugin hooks and returns the final text, and it stops when the adapter raises the
  session's interrupt, as the runner's interrupt monitor does.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Optional

from aiohttp import web


class FakeStore:
    """The gateway session store calls the adapter makes."""

    def __init__(self) -> None:
        self.ids: dict[str, str] = {}
        self.switched: list[tuple[str, str]] = []
        self.models: dict[str, Optional[dict]] = {}

    def get_or_create_session(self, source, force_new=False, touch_activity=True):
        key = f"agent:main:converse:dm:{source.chat_id}"
        self.ids.setdefault(key, f"sid-{source.chat_id}")
        return SimpleNamespace(session_key=key, session_id=self.ids[key])

    def switch_session(self, session_key, target):
        self.switched.append((session_key, target))
        self.ids[session_key] = target
        return SimpleNamespace(session_key=session_key, session_id=target)

    def lookup_by_session_key(self, session_key):
        return self.ids.get(session_key)

    def peek_session_id(self, session_key):
        return self.ids.get(session_key)

    def get_model_override(self, session_key):
        return self.models.get(session_key)


class FakeHermes:
    def __init__(self, api_key: str, *, deltas: tuple[str, ...] = ("Turn it ", "on."),
                 tool_calls: int = 0, reasoning_tokens: tuple[Optional[int], ...] = (),
                 hang: bool = False) -> None:
        self.api_key = api_key
        self.deltas = tuple(deltas)
        self.tool_calls = tool_calls
        self.reasoning_tokens = tuple(reasoning_tokens)  # one provider report per model call
        self.hang = hang
        self.fail: Optional[Exception] = None  # raised by the runner instead of answering
        self.store = FakeStore()
        self.adapter: Any = None
        # Every voice turn the gateway admitted, in order.
        self.turns: list[dict[str, Any]] = []
        self.commands: list[str] = []
        # API sessions that predate gateway conversations, and where compaction moved them.
        self.sessions: dict[str, dict] = {}
        self.continuations: dict[str, str] = {}

    # ── API server ──
    def routes(self) -> list:
        return [
            web.get("/v1/capabilities", self.capabilities),
            web.get("/api/model/options", self.model_options),
            web.get("/api/sessions/{session_id}/messages", self.session_messages),
        ]

    def _auth(self, request: web.Request) -> Optional[web.Response]:
        if request.headers.get("Authorization", "") == f"Bearer {self.api_key}":
            return None
        return web.json_response(
            {"error": {"message": "Invalid gateway API key (API_SERVER_KEY)", "type": "gateway_auth_error",
                       "code": "gateway_auth_failed"}}, status=401)

    async def capabilities(self, request: web.Request) -> web.Response:
        return self._auth(request) or web.json_response(
            {"object": "hermes.api_server.capabilities", "platform": "hermes-agent"})

    async def model_options(self, request: web.Request) -> web.Response:
        return self._auth(request) or web.json_response({"model": "agent", "providers": []})

    async def session_messages(self, request: web.Request) -> web.Response:
        err = self._auth(request)
        if err is not None:
            return err
        sid = request.match_info["session_id"]
        if sid not in self.sessions:
            return web.json_response({}, status=404)
        return web.json_response({"session_id": self.continuations.get(sid, sid), "data": []})

    # ── gateway runner ──
    def install(self, adapter: Any) -> None:
        self.adapter = adapter
        adapter.set_session_store(self.store)
        adapter.set_message_handler(self.handle)

    async def handle(self, event: Any) -> Optional[str]:
        adapter = self.adapter
        key = adapter._source_session_key(event.source)
        if event.get_command() == "model":
            self.commands.append(event.text)
            args = event.get_command_args().split()
            self.store.models[key] = None if args == ["reset"] else {
                "model": args[0], **({"provider": args[2]} if len(args) > 2 else {})}
            return "Model switched."
        override = self.store.models.get(key) or {}
        turn = {"text": event.text, "instructions": event.channel_prompt, "chat_id": event.source.chat_id,
                "chat_name": event.source.chat_name, "user_id": event.source.user_id,
                "user_name": event.source.user_name, "model": override.get("model"),
                "provider": override.get("provider"), "status": "running", "output": ""}
        self.turns.append(turn)
        self.begin(turn)
        if self.fail is not None:
            turn["status"] = "failed"
            raise self.fail

        def interrupted() -> bool:
            return adapter.has_pending_interrupt(key)

        sid = self.store.peek_session_id(key)
        for _ in range(self.tool_calls):
            adapter.on_tool_call(session_id=sid)
        for count in self.reasoning_tokens:
            adapter.on_api_request(session_id=sid, usage={"reasoning_tokens": count})
        adapter.on_stream_start(session_id=sid)
        for delta in self.deltas:
            if interrupted():
                break
            adapter.on_stream_delta(delta=delta, kind="text", session_id=sid)
            await asyncio.sleep(0)
        while self.hang and not interrupted():
            await asyncio.sleep(0.01)
        if interrupted():
            turn["status"] = "interrupted"
            return None
        turn["status"], turn["output"] = "completed", "".join(self.deltas)
        return turn["output"] or None

    def begin(self, turn: dict[str, Any]) -> None:
        """Called as each voice turn starts; a test overrides it to change the script mid-test."""

    @property
    def stopped(self) -> list[dict[str, Any]]:
        return [turn for turn in self.turns if turn["status"] == "interrupted"]
