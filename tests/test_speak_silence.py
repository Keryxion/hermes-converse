"""An explicit silent voice response survives Hermes's empty-response handling."""

from types import SimpleNamespace

import pytest

from hermes_converse.voice_tools import register_tools


def hooks():
    registered = {}
    ctx = SimpleNamespace(register_tool=lambda **kw: None,
                          register_hook=lambda name, fn: registered.update({name: fn}))
    register_tools(ctx, None)
    return registered


def call(h, text="", **changes):
    event = dict(tool_name="speak_this", args={"text": text}, result='{"result": "ok"}',
                 session_id="session", turn_id="turn")
    event.update(changes)
    h["post_tool_call"](**event)


def transform(h, **changes):
    event = dict(response_text="(empty)", session_id="session", turn_id="turn", platform="converse")
    event.update(changes)
    return h["transform_llm_output"](**event)


def test_empty_tool_response_marks_only_its_own_turn_silent():
    h = hooks()
    call(h)
    assert transform(h, turn_id="another") is None
    assert transform(h, session_id="another") is None
    assert transform(h) == "[SILENT]"
    assert transform(h) is None


def test_failed_tool_and_real_response_are_not_suppressed():
    h = hooks()
    call(h, result='{"result": "not_attached"}')
    assert transform(h) is None
    call(h)
    assert transform(h, response_text="A real error occurred.") is None
    assert transform(h) is None


def test_spoken_text_and_other_platforms_are_not_silenced():
    h = hooks()
    call(h)
    call(h, "Hello")
    assert transform(h) is None
    call(h)
    assert transform(h, platform="telegram") is None
    assert transform(h) == "[SILENT]"


def test_cancelled_turn_cannot_silence_a_later_turn():
    h = hooks()
    call(h)
    h["on_session_end"](session_id="session", turn_id="turn", interrupted=True)
    assert transform(h) is None


def test_hermes_transforms_empty_before_gateway_shapes_background_reply(monkeypatch):
    finalizer = pytest.importorskip("agent.turn_finalizer")
    filters = pytest.importorskip("gateway.response_filters")
    h = hooks()
    call(h)
    monkeypatch.setattr(finalizer, "_invoke_hook_safely",
                        lambda name, logger, **kw: [h[name](**kw)])
    agent = SimpleNamespace(session_id="session", model="test", platform="converse")
    reply, changed, _ = finalizer.apply_llm_output_transform(agent, "(empty)", turn_id="turn")
    assert changed and reply == "[SILENT]"
    assert filters.is_intentional_silence_agent_result({"final_response": reply}, reply)
