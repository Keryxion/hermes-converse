import asyncio
import hashlib
import json

import pytest

from hermes_converse import instructions
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


def test_unedited_earlier_default_is_updated_and_an_edited_file_is_kept(tmp_path, monkeypatch, caplog):
    # Changing defaults/CONVERSE.md means adding its hash here, keeping the earlier ones.
    current = DEFAULT_FILE.read_bytes()
    assert hashlib.sha256(current).hexdigest() in instructions.SHIPPED_HASHES_FILE.read_text().split()

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path, earlier = tmp_path / "CONVERSE.md", b"An earlier release's guidance.\n"
    shipped = tmp_path / "shipped"
    shipped.write_text(hashlib.sha256(earlier).hexdigest() + "\n" + hashlib.sha256(current).hexdigest() + "\n")
    monkeypatch.setattr(instructions, "SHIPPED_HASHES_FILE", shipped)
    path.write_bytes(earlier)
    with caplog.at_level("INFO"):
        assert load_voice_guidance() == current.decode("utf-8")
    assert path.read_bytes() == current and str(path) in caplog.text
    assert [entry.name for entry in tmp_path.iterdir() if entry.name.startswith(".CONVERSE")] == []
    path.write_bytes(earlier + b"Speak French.\n")
    assert load_voice_guidance() == (earlier + b"Speak French.\n").decode("utf-8")
    assert path.read_bytes() == earlier + b"Speak French.\n"


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
    assert voice_system_prompt("", "Arya").startswith("Your name is Arya. ")
    assert voice_system_prompt(guidance, "Arya").endswith("\n\n" + guidance)
    device = 'Den "Echo"'
    prompt = voice_system_prompt(guidance, device_name=device, recipient_id="opaque", commands=[])
    prose, identity, catalog = prompt.split("\n\n")
    assert prose == guidance
    labels, _ = json.JSONDecoder().raw_decode(identity.removeprefix("Voice identity supplied by the server: "))
    assert "Cadence's request tool" in identity
    assert labels == {
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
