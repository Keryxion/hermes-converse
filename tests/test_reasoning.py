import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import WSMsgType

from agent.i18n import t
from gateway.slash_commands_model import GatewayModelCommandsMixin

from hermes_converse import reasoning, route
from tests.support.fake_hermes import FakeHermes
from tests.test_route import API_KEY, Harness, _FakeStreamer, _patch_speech, _start_msg


class ReasoningGateway(FakeHermes, GatewayModelCommandsMixin):
    preference = None

    def _peek_session_state(self, key):
        return SimpleNamespace(conversation=SimpleNamespace(reasoning_override=self.preference))

    def _resolve_session_reasoning_config(self, **kwargs):
        return self.preference or {"effort": "low"}

    def _set_session_reasoning_override(self, key, config):
        self.preference = config

    def _evict_cached_agent(self, key):
        pass

    def _load_reasoning_config(self):
        return {"effort": "low"}

    async def handle(self, event):
        if event.get_command() == "model":
            from hermes_cli.model_switch import parse_model_switch_args
            selection = parse_model_switch_args(event.get_command_args())
            if selection.reasoning_effort:
                self._apply_reasoning_selection(self.adapter._source_session_key(event.source),
                                                "converse", selection.reasoning_effort)
        if event.get_command() == "reasoning":
            arg = event.get_command_args()
            if "reasoning" in self.command_replies:
                return self.command_replies["reasoning"]
            return self._apply_reasoning_selection(self.adapter._source_session_key(event.source), "converse", arg)
        return await super().handle(event)

    def begin(self, turn):
        turn["effort"] = self._resolve_session_reasoning_config()["effort"]


async def receive(ws, kind):
    async with asyncio.timeout(10):
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                frame = msg.json()
                if frame["type"] == kind:
                    return frame
    pytest.fail(f"Socket closed before {kind}")


@pytest.mark.parametrize("combined", [True, False])
async def test_reasoning_waits_for_current_run_and_precedes_next(monkeypatch, combined):

    monkeypatch.setattr(reasoning, "capability", lambda *_: ("supported", ["low", "high"]))
    fake = ReasoningGateway(API_KEY, hang=True)
    _patch_speech(monkeypatch, _FakeStreamer([bytes(480)]))
    sessions = []
    resolve = route._resolve_session

    def capture(*args):
        result = resolve(*args)
        sessions.append(result[-1])
        return result

    monkeypatch.setattr(route, "_resolve_session", capture)
    async with Harness(fake, {"voice_model": "example", "voice_provider": "openrouter"}) as http:
        ws = await http.ws_connect("/v1/audio/converse")
        await ws.send_json({**json.loads(_start_msg()), "key": API_KEY})
        state = (await receive(ws, "ready"))["current_model"]
        assert (state["reasoning_override"], state["reasoning_level"]) == (None, "low")
        sessions[0].transcripts.put("first question")
        async with asyncio.timeout(5):
            while not fake.turns:
                await asyncio.sleep(.01)
        command = {"type": "command", "name": "reasoning", "args": ["high"]}
        if combined:
            command = {"type": "command", "name": "model",
                       "args": ["beta", "--provider", "openrouter", "--reasoning", "high", "--session"]}
        await ws.send_json(command)
        await ws.send_json({"type": "current_model"})
        pending = (await receive(ws, "current_model"))["current_model"]
        assert pending["pending"] and pending["reasoning_override"] is None
        await ws.send_json({"type": "command", "name": "model", "args": ["other", "--session"]})
        assert (await receive(ws, "command"))["error"] == "change_pending"
        assert fake.turns[0]["status"] == "running" and fake.stopped == []
        fake.hang = False
        reply = None
        drained = False
        async with asyncio.timeout(10):
            while reply is None or not drained:
                msg = await ws.receive()
                if msg.type != WSMsgType.TEXT:
                    continue
                frame = msg.json()
                if frame["type"] == "turn_done":
                    await ws.send_json({"type": "state", "turn_id": frame["turn_id"], "state": "listening"})
                    drained = True
                elif frame["type"] == "command":
                    reply = frame
        assert reply["current_model"]["reasoning_level"] == "high"
        assert reply["current_model"]["model"] == ("beta" if combined else "example")
        sessions[0].transcripts.put("second question")
        async with asyncio.timeout(8):
            while len(fake.turns) < 2:
                await asyncio.sleep(.01)
        assert [turn["effort"] for turn in fake.turns] == ["low", "high"]
        assert fake.stopped == []
        assert fake.turns[1]["model"] == ("beta" if combined else "example")
        assert '"reasoning_levels":["low","high"]' in fake.turns[1]["instructions"]
        assert '"reasoning_level":"high"' in fake.turns[1]["instructions"]
        await ws.close()


def test_capability_uses_catalog_and_parser(monkeypatch):
    from hermes_cli import models_reasoning_caps as catalog

    from agent.reasoning_effort import codex_supported_efforts
    assert reasoning.capability("gpt-5.6", "openai-codex") == ("supported", list(codex_supported_efforts("gpt-5.6")))
    assert reasoning.capability("example", "other") == ("unknown", [])
    for caps, expected in [
        (None, ("unknown", [])),
        ({"supports_reasoning": True}, ("unknown", [])),
        ({"supports_reasoning": True, "mandatory": True, "supported_efforts": ["none", "high"]}, ("supported", ["high"])),
        ({"supports_reasoning": False}, ("unsupported", [])),
        ({"supports_reasoning": True, "supported_efforts": ["low", "high", "not-a-command"]},
         ("supported", ["low", "high"])),
    ]:
        monkeypatch.setattr(catalog, "openrouter_model_reasoning_capabilities", lambda model: caps)
        assert reasoning.capability("example", "openrouter") == expected


async def test_picker_confirm_refusal_reset_and_reconnect(monkeypatch):

    monkeypatch.setattr(reasoning, "capability", lambda *_: ("supported", ["low", "high"]))
    fake = ReasoningGateway(API_KEY)
    settings = {"voice_model": "example", "voice_provider": "openrouter"}
    start = {**json.loads(_start_msg()), "key": API_KEY}
    select = {"type": "command", "name": "reasoning", "args": ["high"]}
    async with Harness(fake, settings) as http:
        ws = await http.ws_connect("/v1/audio/converse")
        await ws.send_json(start)
        assert not (await receive(ws, "ready"))["current_model"]["pending"]
        await ws.send_json({"type": "command", "name": "model", "args": ["beta", "--provider", "openrouter", "--session"]})
        assert (await receive(ws, "command"))["current_model"]["model"] == "beta"
        await ws.send_json(select)
        assert (await receive(ws, "command"))["current_model"]["reasoning_override"] == "high"
        fake.command_replies["reasoning"] = "Command access denied."
        await ws.send_json({**select, "args": ["low"]})
        refused = await receive(ws, "command")
        assert refused["reply"] == "Command access denied." and refused["current_model"]["reasoning_level"] == "high"
        del fake.command_replies["reasoning"]
        replacement = await http.ws_connect("/v1/audio/converse")
        await replacement.send_json(start)
        state = (await receive(replacement, "ready"))["current_model"]
        assert state["model"] == "beta" and state["reasoning_override"] == "high" and not state["pending"]
        await replacement.send_json({**select, "args": ["reset"]})
        state = (await receive(replacement, "command"))["current_model"]
        assert "error" not in state and state["reasoning_override"] is None and state["reasoning_level"] == "low"
        await replacement.send_json({"type": "command", "name": "model", "args": ["reset"]})
        assert (await receive(replacement, "command"))["current_model"]["model"] == "example"
        await replacement.close()
        await ws.close()


@pytest.mark.parametrize("dispatched", [True, False])
async def test_replacement_fences_reasoning(monkeypatch, dispatched):
    started, release = asyncio.Event(), asyncio.Event()

    class PausedGateway(ReasoningGateway):
        async def handle(self, event):
            if event.get_command() == "reasoning":
                started.set()
                await release.wait()
            return await super().handle(event)

    fake = PausedGateway(API_KEY)
    monkeypatch.setattr(reasoning, "capability", lambda *_: ("supported", ["high"]))
    if not dispatched:
        async def wait_for_run(self):
            started.set()
            await release.wait()
        monkeypatch.setattr(route.GatewayTurns, "wait_for_voice_turn", wait_for_run)
    start = {**json.loads(_start_msg()), "key": API_KEY}
    harness = Harness(fake, {"voice_model": "example", "voice_provider": "openrouter"})
    async with harness as http:
        ws = await http.ws_connect("/v1/audio/converse")
        await ws.send_json(start)
        await receive(ws, "ready")
        await ws.send_json({"type": "command", "name": "reasoning", "args": ["high"]})
        await asyncio.wait_for(started.wait(), 5)
        replacement = await http.ws_connect("/v1/audio/converse")
        await replacement.send_json(start)
        if dispatched:
            old = next(iter(harness.route._named_connections.values()))
            await asyncio.wait_for(old.replaced.wait(), 5)
            release.set()
        state = (await receive(replacement, "ready"))["current_model"]
        assert not state["pending"]
        assert state["reasoning_override"] == ("high" if dispatched else None)
        release.set()
        await replacement.close()
        await ws.close()


@pytest.mark.parametrize("new_session", ["new", "reset"])
async def test_only_new_session_interrupts_active_turn(monkeypatch, new_session):
    fake = ReasoningGateway(API_KEY, hang=True)
    fake.command_replies.update(status="Running.", new="New session.", reset="New session.")
    _patch_speech(monkeypatch, _FakeStreamer([bytes(480)]))
    harness = Harness(fake, {})
    async with harness as http:
        ws = await http.ws_connect("/v1/audio/converse")
        await ws.send_json({**json.loads(_start_msg()), "key": API_KEY})
        assert "current_model" in await receive(ws, "ready")
        connection = next(iter(harness.route._named_connections.values()))
        connection.session.transcripts.put("first turn")
        async with asyncio.timeout(5):
            while not fake.turns:
                await asyncio.sleep(.01)
        await ws.send_json({"type": "command", "name": "status"})
        assert (await receive(ws, "command"))["reply"] == "Running."
        assert fake.turns[0]["status"] == "running" and not fake.stopped
        await ws.send_json({"type": "command", "name": new_session})
        assert "reply" in await receive(ws, "command")
        assert fake.turns[0]["status"] == "interrupted"
        await ws.close()


async def test_submitted_turn_waits_for_model_command_readback(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    class PausedGateway(ReasoningGateway):
        async def handle(self, event):
            if event.get_command() == "model" and event.get_command_args().startswith("beta "):
                started.set()
                await release.wait()
            return await super().handle(event)

    fake = PausedGateway(API_KEY)
    monkeypatch.setattr(reasoning, "capability", lambda *_: ("supported", ["low", "high"]))
    _patch_speech(monkeypatch, _FakeStreamer([bytes(480)]))
    harness = Harness(fake, {"voice_model": "example", "voice_provider": "openrouter"})
    async with harness as http:
        ws = await http.ws_connect("/v1/audio/converse")
        await ws.send_json({**json.loads(_start_msg()), "key": API_KEY})
        await receive(ws, "ready")
        await ws.send_json({"type": "command", "name": "model",
                            "args": ["beta", "--provider", "openrouter", "--reasoning", "high", "--session"]})
        await asyncio.wait_for(started.wait(), 5)
        connection = next(iter(harness.route._named_connections.values()))
        connection.session.transcripts.put("Which model is this?")
        try:
            await receive(ws, "thinking")
            await ws.send_json({"type": "current_model"})
            state = (await receive(ws, "current_model"))["current_model"]
            assert state["pending"] and state["model"] == "example" and not fake.turns
        finally:
            release.set()
        await receive(ws, "turn_done")
        assert [(turn["model"], turn["effort"]) for turn in fake.turns] == [("beta", "high")]
        await ws.close()
