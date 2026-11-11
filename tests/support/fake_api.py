"""A scriptable stand-in for the Hermes API server's Runs API, served by aiohttp in-process.

Implements exactly the surface the plugin uses — ``GET /v1/capabilities`` (the auth probe),
``POST /v1/runs``, ``GET /v1/runs/{id}/events`` (SSE with ``id:`` sequence numbers and
``Last-Event-ID`` replay), ``GET /v1/runs/{id}``, ``POST .../stop``, ``.../approval`` and
``.../clarify`` — with the real wire shapes, so the client and the route are exercised end to end
without a model, a gateway or a session store.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from aiohttp import web


class FakeRunsAPI:
    def __init__(self, api_key: str, *, deltas: tuple[str, ...] = ("Turn it ", "on."),
                 interim: Optional[str] = None, pre_events: tuple[tuple[str, dict], ...] = (),
                 hang: bool = False, drop_after: Optional[int] = None) -> None:
        self.api_key = api_key
        self.deltas = tuple(deltas)
        self.interim = interim
        self.pre_events = tuple(pre_events)
        self.hang = hang
        self.drop_after = drop_after
        self.runs: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list] = {}
        self.continuations: dict[str, str] = {}
        self.approvals: list[dict[str, Any]] = []
        self.clarifies: list[dict[str, Any]] = []
        self.subscriptions: list[tuple[str, Optional[str]]] = []
        self.stop_calls: list[str] = []
        self._n = 0

    # ── wiring ──
    def routes(self) -> list:
        return [
            web.get("/v1/capabilities", self.capabilities),
            web.get("/api/model/options", self.model_options),
            web.post("/api/sessions", self.create_session),
            web.get("/api/sessions", self.list_sessions),
            web.get("/api/sessions/{session_id}", self.get_session),
            web.get("/api/sessions/{session_id}/messages", self.session_messages),
            web.post("/v1/runs", self.create_run),
            web.get("/v1/runs/{run_id}", self.status),
            web.get("/v1/runs/{run_id}/events", self.events),
            web.post("/v1/runs/{run_id}/stop", self.stop),
            web.post("/v1/runs/{run_id}/approval", self.approval),
            web.post("/v1/runs/{run_id}/clarify", self.clarify),
        ]

    async def model_options(self, request):
        return web.json_response({'model': 'agent', 'providers': []})

    def app(self) -> web.Application:
        app = web.Application()
        app.add_routes(self.routes())
        return app

    def _auth(self, request: web.Request) -> Optional[web.Response]:
        header = request.headers.get("Authorization", "")
        if header == f"Bearer {self.api_key}":
            return None
        return web.json_response(
            {"error": {"message": "Invalid gateway API key (API_SERVER_KEY)", "type": "gateway_auth_error",
                       "code": "gateway_auth_failed"}}, status=401)

    # ── handlers ──
    async def capabilities(self, request: web.Request) -> web.Response:
        err = self._auth(request)
        if err is not None:
            return err
        return web.json_response({"object": "hermes.api_server.capabilities", "platform": "hermes-agent",
                                  "features": {"run_submission": True, "realtime_voice": False}})

    async def create_session(self, request):
        err = self._auth(request)
        if err is not None:
            return err
        body = await request.json()
        session_id = body["id"]
        if session_id in self.sessions:
            return web.json_response({'error': {'code': 'session_exists'}}, status=409)
        if body.get('title') and any(s.get('title') == body['title'] for s in self.sessions.values()):
            return web.json_response({'error': {'code': 'invalid_title'}}, status=400)
        self.sessions[session_id] = {"id": session_id, "source": "api_server", 'title': body.get('title')}
        return web.json_response({"session": self.sessions[session_id]}, status=201)

    async def list_sessions(self, request):
        err = self._auth(request)
        if err is not None:
            return err
        return web.json_response({'data': [s for s in self.sessions.values()
                                          if s.get('title') == request.query.get('title')], 'has_more': False})

    async def session_messages(self, request):
        err = self._auth(request)
        if err is not None:
            return err
        sid = request.match_info['session_id']
        if sid not in self.sessions:
            return web.json_response({}, status=404)
        live = self.continuations.get(sid, sid)
        return web.json_response({'session_id': live, 'data': self.messages.get(live, [])[-40:]})

    async def get_session(self, request):
        err = self._auth(request)
        if err is not None:
            return err
        session = self.sessions.get(request.match_info["session_id"])
        return web.json_response({"session": session}, status=200 if session else 404)

    async def create_run(self, request: web.Request) -> web.Response:
        err = self._auth(request)
        if err is not None:
            return err
        body = await request.json()
        self._n += 1
        run_id = f"run_{self._n:04d}"
        self.runs[run_id] = {
            "body": body, "idempotency_key": request.headers.get("Idempotency-Key"),
            "stopped": asyncio.Event(), "status": "running", "output": "", "dropped": False}
        return web.json_response({"run_id": run_id, "status": "started", "replayed": False}, status=202)

    async def status(self, request: web.Request) -> web.Response:
        err = self._auth(request)
        if err is not None:
            return err
        run = self.runs.get(request.match_info["run_id"])
        if run is None:
            return web.json_response({"error": {"message": "unknown run"}}, status=404)
        return web.json_response({"object": "hermes.run", "run_id": request.match_info["run_id"],
                                  "status": run["status"], "output": run["output"]})

    async def stop(self, request: web.Request) -> web.Response:
        err = self._auth(request)
        if err is not None:
            return err
        run_id = request.match_info["run_id"]
        self.stop_calls.append(run_id)
        run = self.runs.get(run_id)
        if run is None:
            return web.json_response({"error": {"message": "unknown run"}}, status=404)
        run["stopped"].set()
        if run["status"] == "running":
            run["status"] = "cancelled"  # the server settles a stopped run whether or not anyone is subscribed
        return web.json_response({"run_id": run_id, "status": "stopping"})

    async def approval(self, request: web.Request) -> web.Response:
        err = self._auth(request)
        if err is not None:
            return err
        self.approvals.append(await request.json())
        return web.json_response({"object": "hermes.run.approval_response"})

    async def clarify(self, request: web.Request) -> web.Response:
        err = self._auth(request)
        if err is not None:
            return err
        self.clarifies.append(await request.json())
        return web.json_response({"run_id": request.match_info["run_id"]})

    def _scripted(self, run_id: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for name, fields in self.pre_events:
            events.append({"event": name, "run_id": run_id, **fields})
        if self.interim is not None:
            events.append({"event": "message.interim", "run_id": run_id, "text": self.interim,
                           "already_streamed": True})
        for delta in self.deltas:
            events.append({"event": "message.delta", "run_id": run_id, "delta": delta})
        return events

    async def events(self, request: web.Request) -> web.StreamResponse:
        err = self._auth(request)
        if err is not None:
            return err
        run_id = request.match_info["run_id"]
        run = self.runs.get(run_id)
        if run is None:
            return web.json_response({"error": {"message": "unknown run"}}, status=404)
        last_header = request.headers.get("Last-Event-ID")
        self.subscriptions.append((run_id, last_header))
        last_seq = int(last_header) if last_header is not None else -1
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream",
                                                        "Cache-Control": "no-cache"})
        await resp.prepare(request)
        await resp.write(b": open\n\n")
        scripted = self._scripted(run_id)
        written = 0
        for seq, event in enumerate(scripted):
            if run["stopped"].is_set():
                break
            if seq <= last_seq:
                continue
            await resp.write(f"id: {seq}\ndata: {json.dumps({**event, 'seq': seq})}\n\n".encode())
            written += 1
            if self.drop_after is not None and not run["dropped"] and written >= self.drop_after:
                run["dropped"] = True
                return resp  # the stream drops without a terminal event
            await asyncio.sleep(0)
        if self.hang and not run["stopped"].is_set():
            await run["stopped"].wait()
        seq = len(scripted)
        if run["stopped"].is_set():
            run["status"] = "cancelled"
            terminal = {"event": "run.cancelled", "run_id": run_id, "completed": False, "interrupted": True}
        else:
            run["status"], run["output"] = "completed", "".join(self.deltas)
            terminal = {"event": "run.completed", "run_id": run_id, "completed": True,
                        "output": run["output"], "usage": {}}
        if seq > last_seq:
            await resp.write(f"id: {seq}\ndata: {json.dumps({**terminal, 'seq': seq})}\n\n".encode())
        await resp.write(b": stream closed\n\n")
        return resp
