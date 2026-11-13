"""Named-device contract: authenticated sockets, persistent device identity, one gateway
conversation per assistant and device."""
import asyncio

import pytest
from aiohttp import web

from hermes_converse.named import DeviceStore, NamedError, parse_names
from tests.test_arbitrate_integration import _audio_turn, _patch
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
    assert same.session_id is None
    reopened.close()


def test_forget_drops_only_that_conversations_earlier_session(tmp_path):
    import sqlite3
    path = tmp_path/'devices.db'
    store = DeviceStore(path)
    arya, nova = store.reserve('Arya', 'Den Echo'), store.reserve('Nova', 'Den Echo')
    with sqlite3.connect(path) as db:
        db.execute("UPDATE conversations SET session_id='voice_' || assistant_name")
    assert store.reserve('Arya', 'Den Echo').session_id == 'voice_Arya'
    store.forget(arya)
    assert store.reserve('Arya', 'Den Echo').session_id is None
    assert store.reserve('Nova', 'Den Echo').session_id == 'voice_Nova' and nova.recipient_id == arya.recipient_id
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
        assert 'pulse' in prompt and 'without a new user' in prompt
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
        assert a['recipient_id'] != b['recipient_id'] and len(fake.store.ids) == 2
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
        assert a['recipient_id'] == b['recipient_id'] and len(fake.store.ids) == 1
        await _audio_turn(second)
        await second.close()


@pytest.mark.asyncio
async def test_replacement_stops_the_turn_in_flight(tmp_path, monkeypatch):
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
        assert [turn['status'] for turn in fake.turns] == ['interrupted']
        fake.hang = False
        await _audio_turn(second)
        await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403, 500])
async def test_failed_lookup_of_an_earlier_conversation_keeps_the_reference(tmp_path, monkeypatch, status):
    import sqlite3
    fake = _patch(monkeypatch)
    path = tmp_path/'devices.db'
    store = DeviceStore(path)
    store.reserve('Arya', 'Den Echo')
    with sqlite3.connect(path) as db:
        db.execute("UPDATE conversations SET session_id='voice_old'")

    async def failing(request):
        return web.json_response({}, status=status)
    fake.session_messages = failing
    async with Harness(fake, device_store=store) as client:
        ws, refused = await connect(client)
        assert refused == {'type': 'error', 'error': 'conversation_unavailable'}
        assert store.reserve('Arya', 'Den Echo').session_id == 'voice_old' and not fake.store.switched


@pytest.mark.asyncio
async def test_wrong_key_cannot_create_device_or_conversation(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    store = DeviceStore(tmp_path/'devices.db')
    async with Harness(fake, device_store=store) as client:
        ws = await client.ws_connect('/v1/audio/converse')
        await ws.send_str(_start_msg(key='wrong', **frame()))
        assert (await ws.receive_json(timeout=2))['error'] == 'unauthorized'
        assert store._db is None and not fake.store.ids
        await ws.close()


def test_prompt_explains_names_proactivity_and_real_scheduling_receipts():
    from hermes_converse.loop import voice_system_prompt
    prompt = voice_system_prompt('Arya', device_name='Den Echo', recipient_id='opaque')
    assert 'Your name is Arya' in prompt and 'Your name is Den Echo' not in prompt
    assert 'physical microphone/speaker' in prompt and 'default destination is this device' in prompt
    assert 'persistent conversation' in prompt and 'without a new user message or wake word' in prompt
    assert 'pulse tool is available' in prompt and 'tool\'s receipt' in prompt
    assert 'Cancelling speech does not cancel a timer' in prompt
    assert 'If a tool or delivery integration is unavailable' in prompt


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
        assert len(fake.store.ids) == 1
        assert len({r['recipient_id'] for _, r in results}) == 1
        assert len(harness.route.hub._attachments) == 1
        for ws, _ in results:
            await ws.close()
