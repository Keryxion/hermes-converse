import asyncio
import json

import pytest

from hermes_converse.instructions import DEFAULT_FILE, load_voice_guidance, voice_system_prompt


async def test_concurrent_profile_initialization_and_edits(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "CONVERSE.md"
    expected = DEFAULT_FILE.read_text(encoding="utf-8")
    assert await asyncio.gather(*(asyncio.to_thread(load_voice_guidance) for _ in range(8))) == [expected] * 8
    for edited in ["Speak French.", "", "   \n"]:
        path.write_text(edited, encoding="utf-8")
        assert load_voice_guidance() == edited
    other = tmp_path / "other"
    monkeypatch.setenv("HERMES_HOME", str(other))
    assert load_voice_guidance() == expected
    assert path.read_text(encoding="utf-8") == "   \n"


@pytest.mark.parametrize("unreadable", ["directory", "invalid UTF-8"])
def test_failed_read_preserves_profile_file(tmp_path, monkeypatch, caplog, unreadable):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "CONVERSE.md"
    if unreadable == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"\xff")
    assert load_voice_guidance() == DEFAULT_FILE.read_text(encoding="utf-8")
    assert str(path) in caplog.text
    if unreadable == "directory":
        assert path.is_dir()
    else:
        assert path.read_bytes() == b"\xff"


def test_custom_prose_and_generated_context():
    guidance = "Use {{literal braces}}."
    assert voice_system_prompt(guidance) == guidance
    assert voice_system_prompt("", "Arya") == 'Assistant name: "Arya"'
    device = 'Den "Echo"'
    prompt = voice_system_prompt(guidance, device_name=device, recipient_id="opaque", commands=[])
    prose, identity, catalog = prompt.split("\n\n")
    assert prose == guidance
    assert json.loads(identity.removeprefix("Voice identity supplied by the server: ")) == {
        "device_name": device, "assistant_name": "", "conversation_name": device, "recipient_id": "opaque"}
    assert catalog == "Gateway command catalog: []"


def test_ordinary_hermes_context_only_loads_persona(tmp_path, monkeypatch):
    from agent.prompt_builder import build_context_files_prompt

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "SOUL.md").write_text("General persona guidance.", encoding="utf-8")
    (tmp_path / "CONVERSE.md").write_text("Voice-specific guidance.", encoding="utf-8")
    prompt = build_context_files_prompt(cwd=str(tmp_path))
    assert "General persona guidance." in prompt
    assert "Voice-specific guidance." not in prompt
