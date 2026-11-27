import asyncio
import json
from types import SimpleNamespace

import pytest

from hermes_converse.admin import Admin, _Attached
from hermes_converse.voice_tools import make_handlers


@pytest.mark.parametrize("value", [True, -0.1, 1.1, "0.5", float("nan")])
async def test_controls_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setattr("gateway.session_context.get_session_env", lambda key, default="":
                        "converse" if key == "HERMES_SESSION_PLATFORM" else "chat")
    attachment = SimpleNamespace(controls=lambda value: None)
    hub = SimpleNamespace(attachment=lambda _: attachment)
    result = json.loads(await make_handlers(hub)["controls"]({"volume": value}))
    assert "error" in result


async def test_controls_get_set_and_device_scope(monkeypatch):
    env = {"HERMES_SESSION_PLATFORM": "converse", "HERMES_SESSION_CHAT_ID": "chat"}
    monkeypatch.setattr("gateway.session_context.get_session_env", lambda key, default="": env.get(key, default))
    calls = []
    def volume(value):
        calls.append(value)
        future = asyncio.get_running_loop().create_future()
        future.set_result({"volume": 0.4})
        return future
    hub = SimpleNamespace(attachment=lambda _: SimpleNamespace(controls=volume))
    handler = make_handlers(hub)["controls"]
    assert json.loads(await handler({})) == {"volume": 0.4}
    assert json.loads(await handler({"volume": 0.5})) == {"volume": 0.4}
    assert calls == [{}, {"volume": 0.5}]
    env["HERMES_SESSION_PLATFORM"] = "telegram"
    assert json.loads(await handler({})) == {"result": "not_a_device"}


async def test_admin_volume_uses_device_report_and_attachment_identity():
    admin = Admin(None, None, lambda: "")
    attachment = object()
    class Socket:
        async def send_json(self, frame):
            assert frame == {"type": "controls", "volume": 0.5}
            admin.reported("room", attachment, {"volume": 0.4})
    admin._attached["room"] = _Attached(attachment, Socket())
    assert await admin.controls("room", attachment, {}) == {"muted": None, "volume": None}
    assert await admin.controls("room", attachment, {"volume": 0.5}) == {"muted": None, "volume": 0.4, "reported": True}
    assert await admin.controls("room", object(), {"volume": 0.5}) == {"result": "not_attached"}


async def test_controls_tool_through_voice_socket(monkeypatch):
    from tests.test_admin import _device
    from tests.test_route import API_KEY, Harness, _FakeStreamer, _patch_speech
    from tests.support.fake_hermes import FakeHermes

    _patch_speech(monkeypatch, _FakeStreamer([b"\x01\x02"]))
    harness = Harness(FakeHermes(API_KEY))
    async with harness as client:
        ws = await _device(client, "Kitchen")
        chat, = harness.route.hub._attachments
        monkeypatch.setattr("gateway.session_context.get_session_env", lambda key, default="":
                            {"HERMES_SESSION_PLATFORM": "converse", "HERMES_SESSION_CHAT_ID": chat}.get(key, default))
        handler = make_handlers(harness.route.hub)["controls"]
        task = asyncio.create_task(handler({"volume": 0.7}))
        assert await ws.receive_json(timeout=2) == {"type": "controls", "volume": 0.7}
        await ws.send_json({"type": "controls", "muted": False, "volume": 0.6})
        assert json.loads(await task) == {"muted": False, "volume": 0.6, "reported": True}
        assert json.loads(await handler({})) == {"muted": False, "volume": 0.6}
        await ws.close()


async def test_controls_timeout_does_not_claim_confirmation(monkeypatch):
    from hermes_converse import admin as admin_module

    monkeypatch.setattr(admin_module, "SET_TIMEOUT", 0.01)
    admin = Admin(None, None, lambda: "")
    attachment = object()
    class Socket:
        async def send_json(self, frame):
            pass
    admin._attached["room"] = _Attached(attachment, Socket(), volume=0.5)
    assert await admin.controls("room", attachment, {"volume": 0.5}) == {"muted": None, "volume": 0.5, "reported": False}
    assert not admin._attached["room"].waiters


async def test_mute_and_volume_send_immediately_through_voice_socket(monkeypatch):
    from tests.test_admin import _device
    from tests.test_route import API_KEY, Harness, _FakeStreamer, _patch_speech
    from tests.support.fake_hermes import FakeHermes
    _patch_speech(monkeypatch, _FakeStreamer([b"\x01\x02"]))
    harness = Harness(FakeHermes(API_KEY))
    async with harness as client:
        ws = await _device(client, "Kitchen")
        chat, = harness.route.hub._attachments
        monkeypatch.setattr("gateway.session_context.get_session_env", lambda key, default="":
                            {"HERMES_SESSION_PLATFORM": "converse", "HERMES_SESSION_CHAT_ID": chat}.get(key, default))
        handler = make_handlers(harness.route.hub)["controls"]
        task = asyncio.create_task(handler({"muted": True, "volume": 0.3}))
        assert await ws.receive_json(timeout=1) == {"type": "controls", "muted": True, "volume": 0.3}
        assert not task.done()
        await ws.send_json({"type": "controls", "muted": True, "volume": 0.3})
        assert json.loads(await task) == {"muted": True, "volume": 0.3, "reported": True}
        task = asyncio.create_task(handler({"muted": False}))
        assert await ws.receive_json(timeout=1) == {"type": "controls", "muted": False}
        await ws.send_json({"type": "controls", "muted": False, "volume": 0.3})
        assert json.loads(await task) == {"muted": False, "volume": 0.3, "reported": True}
        await ws.close()


async def test_controls_validation_and_registration(monkeypatch):
    from hermes_converse.voice_tools import SCHEMAS
    monkeypatch.setattr("gateway.session_context.get_session_env", lambda key, default="":
                        "converse" if key == "HERMES_SESSION_PLATFORM" else "chat")
    hub = SimpleNamespace(attachment=lambda _: SimpleNamespace(controls=lambda value: None))
    handlers = make_handlers(hub)
    assert "controls" in SCHEMAS and "mute" not in handlers and "volume" not in handlers
    assert "error" in json.loads(await handlers["controls"]({"muted": 1}))
    assert "error" in json.loads(await handlers["controls"]({"extra": True}))


async def test_partial_report_does_not_confirm_both_fields():
    admin = Admin(None, None, lambda: "")
    attachment = object()
    class Socket:
        async def send_json(self, frame):
            admin.reported("room", attachment, {"volume": 0.4})
    admin._attached["room"] = _Attached(attachment, Socket())
    result = await admin.controls("room", attachment, {"muted": True, "volume": 0.4})
    assert result == {"muted": None, "volume": 0.4, "reported": False}
