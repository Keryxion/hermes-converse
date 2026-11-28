"""Named-device contract: authenticated sockets, persistent device identity, one gateway
conversation per assistant and device."""
import asyncio

import pytest

from hermes_converse.named import DeviceStore, NamedError, parse_names
from tests.test_arbitrate_integration import _audio_turn, _collect_turn, _patch
from tests.test_route import API_KEY, Harness, _start_msg, _speech_then_silence_pcm


def frame(**overrides):
    return {'assistant_name': 'Arya', 'device_name': 'Den Echo', **overrides}


async def connect(client, **overrides):
    ws = await client.ws_connect('/v1/audio/converse')
    await ws.send_str(_start_msg(key=API_KEY, transcripts=True, **frame(**overrides)))
    return ws, await ws.receive_json(timeout=3)


@pytest.mark.parametrize('fields', [
    {'device_name': ''}, {'assistant_name': 42}, {'device_name': 'Den\nEcho'},
    {'device_name': 'x'*99}, {'assistant_name': 'x'*60, 'device_name': 'y'*60},
    {'assistant_name': None}, {'device_name': None}, {'conversation': {}},
    {'conversation_id': 'other'}, {'name': 'old'}, {'profile': 'other'},
])
def test_named_start_rejects_malformed_fields_and_overrides(fields):
    with pytest.raises(NamedError, match='bad_start'):
        parse_names(frame(**fields))


@pytest.mark.parametrize('missing', ['assistant_name', 'device_name'])
def test_both_names_are_required_together(missing):
    start = frame()
    start.pop(missing)
    with pytest.raises(NamedError, match='bad_start'):
        parse_names(start)


def test_store_keeps_device_identity_and_separate_assistant_conversations(tmp_path):
    path = tmp_path/'devices.db'
    store = DeviceStore(path)
    first = store.reserve('Arya', 'Den Echo')
    store.close()
    reopened = DeviceStore(path)
    same = reopened.reserve('Arya', 'Den Echo')
    other_assistant = reopened.reserve('Nova', 'Den Echo')
    other_device = reopened.reserve('Arya', 'Kitchen')
    assert same.recipient_id == first.recipient_id and same.candidate_id == first.candidate_id
    assert other_assistant.recipient_id == first.recipient_id
    assert other_assistant.candidate_id != first.candidate_id
    assert other_device.recipient_id != first.recipient_id
    reopened.close()



def test_store_accepts_a_table_a_newer_branch_extended(tmp_path):
    """An alpha deploy of another branch can add columns; this one must still open devices."""
    import sqlite3
    path = tmp_path/'devices.db'
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE devices (device_name TEXT PRIMARY KEY, recipient_id TEXT NOT NULL UNIQUE,
                              settings TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE conversations (recipient_id TEXT NOT NULL, assistant_name TEXT NOT NULL,
                                    title TEXT NOT NULL UNIQUE, session_id TEXT UNIQUE,
                                    PRIMARY KEY(recipient_id, assistant_name));
    """)
    db.close()
    store = DeviceStore(path)
    assert store.reserve('Arya', 'Den Echo').recipient_id == store.reserve('Arya', 'Den Echo').recipient_id
    store.close()

@pytest.mark.asyncio
async def test_names_select_the_conversation_without_exposing_it(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    async with Harness(fake, device_store=DeviceStore(tmp_path/'devices.db')) as client:
        ws, ready = await connect(client)
        assert ready['type'] == 'ready' and ready['assistant_name'] == 'Arya'
        assert 'conversation_id' not in ready and 'extensions' not in ready
        assert ready['device_name'] == 'Den Echo' and ready['recipient_id']
        await _audio_turn(ws)
        (turn,) = fake.turns
        assert turn['chat_name'] == 'Arya: Den Echo' and turn['chat_id'] != ready['session_id']
        prompt = turn['instructions']
        assert 'Your name is Arya' in prompt
        assert '"assistant_name": "Arya"' in prompt and '"device_name": "Den Echo"' in prompt
        assert 'physical microphone/speaker' in prompt and ready['recipient_id'] in prompt
        assert "Cadence's tools" in prompt and 'without a new user' in prompt
        await ws.close()


@pytest.mark.asyncio
async def test_restart_keeps_the_same_conversation(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    for _ in range(2):  # a new route and device store, as after a gateway restart
        async with Harness(fake, device_store=DeviceStore(tmp_path/'devices.db')) as client:
            ws, _ = await connect(client)
            await _audio_turn(ws)
            await ws.close()
        _patch(monkeypatch)
    assert len({turn['chat_id'] for turn in fake.turns}) == 1 and len(fake.turns) == 2


@pytest.mark.asyncio
async def test_two_devices_sharing_assistant_keep_separate_conversations(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    async with Harness(fake, device_store=DeviceStore(tmp_path/'devices.db')) as client:
        first, a = await connect(client)
        second, b = await connect(client, device_name='Kitchen')
        assert a['recipient_id'] != b['recipient_id'] and not fake.store.ids
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_replacement_closes_old_socket_before_admitting_new_audio(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    async with Harness(fake, device_store=DeviceStore(tmp_path/'devices.db')) as client:
        first, a = await connect(client)
        closing = asyncio.create_task(first.receive(timeout=3))
        second, b = await connect(client)
        assert b['type'] == 'ready'
        assert (await closing).data == 4409
        assert first.close_code == 4409
        assert a['session_id'] != b['session_id']
        assert a['recipient_id'] == b['recipient_id'] and not fake.store.ids
        await _audio_turn(second)
        await second.close()


@pytest.mark.asyncio
async def test_replacement_keeps_the_turn_in_flight(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    fake.hang = True
    async with Harness(fake, device_store=DeviceStore(tmp_path/'devices.db')) as client:
        first, _ = await connect(client)
        for chunk in _speech_then_silence_pcm():
            await first.send_bytes(chunk)
        while (await first.receive_json(timeout=2))['type'] != 'thinking':
            pass
        for _ in range(100):
            if fake.turns:
                break
            await asyncio.sleep(.01)
        second, ready = await connect(client)
        assert ready['type'] == 'ready'
        assert [turn['status'] for turn in fake.turns] == ['running']  # the turn carries on
        fake.after_hang = (' More.',)
        fake.hang = False
        frames, pcm = await _collect_turn(second)  # the rest of the turn arrives here
        assert [f['type'] for f in frames if f['type'] != 'transcript'][-1] == 'turn_done' and pcm
        # Text prepared before the switch is only replayed; audio starts with text prepared after it.
        said = [f for f in frames if f['type'] == 'transcript' and f['role'] == 'assistant' and f['text']]
        assert [(f['text'], f.get('replay', False)) for f in said] == [('Hello.', True), (' More.', False)]
        assert frames.index(said[1]) < [f['type'] for f in frames].index('speaking')
        await second.close()


@pytest.mark.asyncio
async def test_wrong_key_cannot_create_device_or_conversation(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    store = DeviceStore(tmp_path/'devices.db')
    async with Harness(fake, device_store=store) as client:
        ws = await client.ws_connect('/v1/audio/converse')
        await ws.send_str(_start_msg(key='wrong', **frame()))
        assert (await ws.receive_json(timeout=2))['error'] == 'unauthorized'
        assert not store.settings() and not fake.store.ids
        await ws.close()


def test_prompt_explains_names_proactivity_and_real_scheduling_receipts():
    from hermes_converse.instructions import DEFAULT_FILE, voice_system_prompt
    guidance = DEFAULT_FILE.read_text(encoding='utf-8')
    prompt = ' '.join(voice_system_prompt(guidance, 'Arya', device_name='Den Echo', recipient_id='opaque').split())
    assert 'Your name is Arya' in prompt and 'Your name is Den Echo' not in prompt
    assert 'physical microphone/speaker' in prompt and 'default destination is this device' in prompt
    assert 'persistent conversation' in prompt and 'without a new user message or wake word' in prompt
    assert "Cadence's tools are available, investigate them" in prompt and 'be proactive' in prompt
    assert 'use the countdown tool' in prompt and 'set a timer for ten minutes' in prompt   # not pulse, for a timer
    assert 'If a tool or delivery integration is unavailable' in prompt
    # Each device's conversation names itself after the device so Cadence requests can reach it.
    assert "request tool is available" in prompt and "action rename and name set to conversation_name" in prompt
    assert '"conversation_name": "Arya: Den Echo"' in prompt          # the conversation's own title, always
    assert "add the assistant_name" not in prompt                      # no fallback name to drift to
    assert "request ask" in prompt and "only after it answers accepted" in prompt
    # One instruction per ask state (Keryxion/cadence#12): the model never reports a refused ask as asked.
    for state in ("awaiting_answer:", "not_delivered:", "unknown:"):
        assert state in prompt
    assert "reachable is false" in prompt
    assert "request tool" not in voice_system_prompt(guidance, 'Arya')   # no device, nothing to name it after
    long = voice_system_prompt(guidance, 'Arya', device_name='Upstairs Guest Bedroom Echo Show Fifteen')
    assert '"conversation_name": "Arya: Upstairs Guest Bedroom Echo Show F"' in long   # Cadence allows 40


def test_distinct_name_pairs_cannot_share_an_ambiguous_title(tmp_path):
    store = DeviceStore(tmp_path/'devices.db')
    store.reserve('A: B', 'C')
    with pytest.raises(NamedError):
        store.reserve('A', 'B: C')
    store.close()


@pytest.mark.asyncio
async def test_concurrent_first_connections_create_one_conversation(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    harness = Harness(fake, device_store=DeviceStore(tmp_path/'devices.db'))
    async with harness as client:
        results = await asyncio.gather(connect(client), connect(client))
        assert all(ready['type'] == 'ready' for _, ready in results)
        assert not fake.store.ids
        assert len({r['recipient_id'] for _, r in results}) == 1
        assert len(harness.route.hub._attachments) == 1
        for ws, _ in results:
            await ws.close()
