import asyncio
import json
from types import SimpleNamespace

import pytest

from hermes_converse.admin import Admin, _Attached
from hermes_converse.voice_tools import make_handlers


@pytest.mark.parametrize("value", [True, -0.1, 1.1, "0.5", float("nan")])
async def test_volume_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setattr("gateway.session_context.get_session_env", lambda key, default="":
                        "converse" if key == "HERMES_SESSION_PLATFORM" else "chat")
    attachment = SimpleNamespace(volume=lambda value: None)
    hub = SimpleNamespace(attachment=lambda _: attachment)
    result = json.loads(await make_handlers(hub)["volume"]({"level": value}))
    assert "error" in result


async def test_volume_get_set_and_device_scope(monkeypatch):
    env = {"HERMES_SESSION_PLATFORM": "converse", "HERMES_SESSION_CHAT_ID": "chat"}
    monkeypatch.setattr("gateway.session_context.get_session_env", lambda key, default="": env.get(key, default))
    calls = []
    def volume(value):
        calls.append(value)
        future = asyncio.get_running_loop().create_future()
        future.set_result({"volume": 0.4})
        return future
    hub = SimpleNamespace(attachment=lambda _: SimpleNamespace(volume=volume))
    handler = make_handlers(hub)["volume"]
    assert json.loads(await handler({})) == {"volume": 0.4}
    assert json.loads(await handler({"level": 0.5})) == {"volume": 0.4}
    assert calls == [None, 0.5]
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
    assert await admin.volume("room", attachment, None) == {"volume": None}
    assert await admin.volume("room", attachment, 0.5) == {"volume": 0.4, "reported": True}
    assert await admin.volume("room", object(), 0.5) == {"result": "not_attached"}


async def test_volume_tool_through_voice_socket(monkeypatch):
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
        handler = make_handlers(harness.route.hub)["volume"]
        task = asyncio.create_task(handler({"level": 0.7}))
        assert await ws.receive_json(timeout=2) == {"type": "controls", "volume": 0.7}
        await ws.send_json({"type": "controls", "muted": False, "volume": 0.6})
        assert json.loads(await task) == {"volume": 0.6, "reported": True}
        assert json.loads(await handler({})) == {"volume": 0.6}
        await ws.close()


async def test_volume_timeout_does_not_claim_confirmation(monkeypatch):
    from hermes_converse import admin as admin_module

    monkeypatch.setattr(admin_module, "SET_TIMEOUT", 0.01)
    admin = Admin(None, None, lambda: "")
    attachment = object()
    class Socket:
        async def send_json(self, frame):
            pass
    admin._attached["room"] = _Attached(attachment, Socket(), volume=0.5)
    assert await admin.volume("room", attachment, 0.5) == {"volume": 0.5, "reported": False}
    assert not admin._attached["room"].waiters
