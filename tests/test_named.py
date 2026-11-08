"""Named-device contract over authenticated HTTP/WebSockets and persistent routing metadata."""
import asyncio

import pytest
from aiohttp import web

from hermes_converse import route, speech
from hermes_converse.named import DeviceStore, NamedError, parse_names
from tests.test_arbitrate_integration import _audio_turn, _patch, _speech_seams
from tests.test_route import API_KEY, Harness, _start_msg, _speech_then_silence_pcm, _collect_turn


def frame(**overrides):
    return {'assistant_name': 'Alexa', 'device_name': 'Den Echo', 'extensions': {'arya-v1': {}}, **overrides}


async def connect(client, **overrides):
    ws = await client.ws_connect('/v1/audio/converse')
    await ws.send_str(_start_msg(key=API_KEY, **frame(**overrides)))
    return ws, await ws.receive_json(timeout=3)


@pytest.mark.parametrize('fields', [
    {'device_name': ''}, {'assistant_name': 42}, {'device_name': 'Den\nEcho'},
    {'device_name': 'x'*99}, {'assistant_name': 'x'*60, 'device_name': 'y'*60},
    {'extensions': []}, {'extensions': {'arya-v1': {'attachment_token': 'wrong'}}},
    {'extensions': {'arya-v1': None}}, {'extensions': {'arya-v1': []}},
    {'extensions': {}, 'device_name': 'Den Echo'}, {'conversation': {}},
    {'conversation_id': 'other'}, {'name': 'old'}, {'profile': 'other'},
])
def test_named_start_rejects_malformed_fields_and_overrides(fields):
    with pytest.raises(NamedError, match='bad_extension'):
        parse_names(frame(**fields))


def test_store_keeps_device_identity_and_separate_assistant_references(tmp_path):
    path = tmp_path/'devices.db'
    store = DeviceStore(path)
    first = store.reserve('Alexa', 'Den Echo')
    store.remember(first, 'saved')
    store.close()
    reopened = DeviceStore(path)
    same = reopened.reserve('Alexa', 'Den Echo')
    other_assistant = reopened.reserve('Arya', 'Den Echo')
    other_device = reopened.reserve('Alexa', 'Kitchen')
    assert same.recipient_id == first.recipient_id and same.session_id == 'saved'
    assert other_assistant.recipient_id == first.recipient_id and other_assistant.session_id is None
    assert other_device.recipient_id != first.recipient_id and other_device.session_id is None
    assert same.candidate_id == first.candidate_id
    reopened.close()


@pytest.mark.asyncio
async def test_names_select_title_and_identity_prompt_without_exposing_conversation(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    async with Harness(fake, device_store=DeviceStore(tmp_path/'devices.db')) as client:
        ws, ready = await connect(client)
        assert ready['type'] == 'ready'
        assert 'conversation_id' not in ready
        recipient = ready['extensions']['arya-v1']
        assert recipient['device_name'] == 'Den Echo' and recipient['recipient_id']
        assert len(fake.sessions) == 1
        cid, saved = next(iter(fake.sessions.items()))
        assert saved['title'] == 'Alexa: Den Echo' and cid != ready['session_id']
        await _audio_turn(ws)
        body = next(iter(fake.runs.values()))['body']
        assert body['session_id'] == cid
        prompt = body['instructions']
        assert 'Your name is Alexa' in prompt
        assert '"assistant_name": "Alexa"' in prompt and '"device_name": "Den Echo"' in prompt
        assert 'physical microphone/speaker' in prompt and recipient['recipient_id'] in prompt
        assert 'pulse' in prompt and 'without a new user' in prompt
        await ws.close()


@pytest.mark.asyncio
async def test_existing_title_is_reused_and_restart_keeps_same_history(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    fake.sessions['existing'] = {'id': 'existing', 'source': 'api_server', 'title': 'Alexa: Den Echo'}
    fake.messages['existing'] = [{'role': 'user', 'content': 'Remember the blue lamp.'},
                                 {'role': 'assistant', 'content': 'I will.'}]
    path = tmp_path/'devices.db'
    recipient = None
    for _ in range(2):
        async with Harness(fake, device_store=DeviceStore(path)) as client:
            ws, ready = await connect(client)
            assert ready['type'] == 'ready'
            current = ready['extensions']['arya-v1']['recipient_id']
            assert recipient is None or recipient == current
            recipient = current
            await _audio_turn(ws)
            assert all(run['body']['session_id'] == 'existing' for run in fake.runs.values())
            await ws.close()
    assert list(fake.sessions) == ['existing']


@pytest.mark.asyncio
async def test_two_devices_sharing_assistant_keep_separate_conversations(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    async with Harness(fake, device_store=DeviceStore(tmp_path/'devices.db')) as client:
        first, a = await connect(client)
        second, b = await connect(client, device_name='Kitchen')
        assert a['extensions']['arya-v1']['recipient_id'] != b['extensions']['arya-v1']['recipient_id']
        assert {s['title'] for s in fake.sessions.values()} == {'Alexa: Den Echo', 'Alexa: Kitchen'}
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
        assert a['extensions'] == b['extensions'] and len(fake.sessions) == 1
        await _audio_turn(second)
        await second.close()


@pytest.mark.asyncio
async def test_replacement_stops_and_settles_inflight_run(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    fake.hang = True
    async with Harness(fake, device_store=DeviceStore(tmp_path/'devices.db')) as client:
        first, _ = await connect(client)
        for chunk in _speech_then_silence_pcm():
            await first.send_bytes(chunk)
        while (await first.receive_json(timeout=2))['type'] != 'thinking':
            pass
        for _ in range(100):
            if fake.runs:
                break
            await asyncio.sleep(.01)
        second, ready = await connect(client)
        assert ready['type'] == 'ready' and fake.stop_calls
        assert all(run['status'] == 'cancelled' for run in fake.runs.values())
        fake.hang = False
        await _audio_turn(second)
        await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403, 500])
async def test_failed_lookup_does_not_create_history(tmp_path, monkeypatch, status):
    fake = _patch(monkeypatch)
    async def failed(request):
        return web.json_response({'error': 'unavailable'}, status=status)
    fake.list_sessions = failed
    async with Harness(fake, device_store=DeviceStore(tmp_path/'devices.db')) as client:
        ws, error = await connect(client)
        assert error['error'] == 'conversation_unavailable'
        await ws.receive(timeout=2)
        assert ws.close_code == 4404 and fake.sessions == {}


@pytest.mark.asyncio
async def test_deleted_saved_history_is_not_replaced(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    path = tmp_path/'devices.db'
    async with Harness(fake, device_store=DeviceStore(path)) as client:
        ws, _ = await connect(client)
        await ws.close()
    fake.sessions.clear()
    async with Harness(fake, device_store=DeviceStore(path)) as client:
        ws, error = await connect(client)
        assert error['error'] == 'conversation_unavailable' and fake.sessions == {}
        await ws.close()


@pytest.mark.asyncio
async def test_compressed_conversation_uses_public_continuation_and_routes_background_output(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    store = DeviceStore(tmp_path/'devices.db')
    dev = store.reserve('Alexa', 'Den Echo')
    store.remember(dev, 'root')
    fake.sessions['root'] = {'id': 'root', 'source': 'api_server', 'end_reason': 'compression'}
    fake.sessions['tip'] = {'id': 'tip', 'source': 'api_server', 'title': 'Alexa: Den Echo', 'parent_session_id': 'root'}
    fake.continuations['root'] = 'tip'
    harness = Harness(fake, device_store=store)
    async with harness as client:
        ws, ready = await connect(client)
        assert ready['type'] == 'ready'
        harness.route.hub.on_response(session_id='tip', task_id='root', turn_id='background',
                                     assistant_response='Your reminder is due.')
        frames, pcm = await _collect_turn(ws)
        assert pcm and frames[0]['event_id'] == 'turn:background' and not fake.runs
        # Compression during a turn keeps the API task identity and can add another live alias.
        harness.route.hub.on_response(session_id='next-tip', task_id='root', turn_id='next',
                                     assistant_response='Another update.')
        frames, pcm = await _collect_turn(ws)
        assert pcm and frames[0]['event_id'] == 'turn:next'
        await ws.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('row', [
    {'id': 'existing', 'source': 'telegram', 'title': 'Alexa: Den Echo'},
    {'id': 'existing', 'source': 'api_server', 'title': 'Alexa: Den Echo', 'hidden': True},
    {'id': 'existing', 'source': 'api_server', 'title': 'Alexa: Den Echo', 'archived': True},
    {'id': 'existing', 'source': 'api_server', 'title': 'Alexa: Den Echo', 'is_internal_child': True},
])
async def test_incompatible_title_never_creates_parallel_history(tmp_path, monkeypatch, row):
    fake = _patch(monkeypatch)
    fake.sessions['existing'] = row
    async with Harness(fake, device_store=DeviceStore(tmp_path/'devices.db')) as client:
        ws, error = await connect(client)
        assert error['error'] == 'conversation_unavailable'
        assert list(fake.sessions) == ['existing']
        await ws.close()


@pytest.mark.asyncio
async def test_title_create_conflict_rereads_winner(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    async def conflicting_create(request):
        body = await request.json()
        fake.sessions['winner'] = {'id': 'winner', 'source': 'api_server', 'title': body['title']}
        return web.json_response({'error': {'code': 'invalid_title'}}, status=400)
    fake.create_session = conflicting_create
    async with Harness(fake, device_store=DeviceStore(tmp_path/'devices.db')) as client:
        ws, ready = await connect(client)
        assert ready['type'] == 'ready'
        await _audio_turn(ws)
        assert next(iter(fake.runs.values()))['body']['session_id'] == 'winner'
        await ws.close()


@pytest.mark.asyncio
async def test_unsupported_title_filter_fails_closed(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    async def unfiltered(request):
        return web.json_response({'data': [{'id': 'wrong', 'source': 'api_server', 'title': 'Other'}]})
    fake.list_sessions = unfiltered
    async with Harness(fake, device_store=DeviceStore(tmp_path/'devices.db')) as client:
        ws, error = await connect(client)
        assert error['error'] == 'conversation_unavailable' and not fake.sessions
        await ws.close()


@pytest.mark.asyncio
async def test_wrong_key_cannot_create_device_or_history(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    store = DeviceStore(tmp_path/'devices.db')
    async with Harness(fake, device_store=store) as client:
        ws = await client.ws_connect('/v1/audio/converse')
        await ws.send_str(_start_msg(key='wrong', **frame()))
        assert (await ws.receive_json(timeout=2))['error'] == 'unauthorized'
        assert store._db is None and not fake.sessions
        await ws.close()


@pytest.mark.asyncio
async def test_replacement_waits_for_delayed_admission_then_stops_it(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    fake.hang = True
    accepted, release = asyncio.Event(), asyncio.Event()
    create = fake.create_run
    async def delayed_create(request):
        response = await create(request)
        accepted.set()
        await release.wait()
        return response
    fake.create_run = delayed_create
    async with Harness(fake, device_store=DeviceStore(tmp_path/'devices.db')) as client:
        first, _ = await connect(client)
        for chunk in _speech_then_silence_pcm():
            await first.send_bytes(chunk)
        while (await first.receive_json(timeout=2))['type'] != 'thinking':
            pass
        await asyncio.wait_for(accepted.wait(), 2)
        replacement = asyncio.create_task(connect(client))
        await asyncio.sleep(.05)
        assert not replacement.done()
        release.set()
        second, ready = await replacement
        assert ready['type'] == 'ready'
        assert fake.stop_calls and all(r['status'] == 'cancelled' for r in fake.runs.values())
        await second.close()


@pytest.mark.asyncio
async def test_unsettled_run_refuses_replacement_until_terminal(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    fake.hang = True
    async def stop_still_settling(request):
        rid = request.match_info['run_id']
        fake.stop_calls.append(rid)
        fake.runs[rid]['stopped'].set()
        return web.json_response({'run_id': rid, 'status': 'stopping'})
    fake.stop = stop_still_settling
    async with Harness(fake, device_store=DeviceStore(tmp_path/'devices.db')) as client:
        first, _ = await connect(client)
        for chunk in _speech_then_silence_pcm():
            await first.send_bytes(chunk)
        while (await first.receive_json(timeout=2))['type'] != 'thinking':
            pass
        while not fake.runs:
            await asyncio.sleep(.01)
        second, error = await connect(client)
        assert error['error'] == 'conversation_busy'
        assert len(fake.runs) == 1
        await second.close()
        for run in fake.runs.values():
            run['status'] = 'cancelled'
        third, ready = await connect(client)
        assert ready['type'] == 'ready'
        await third.close()


@pytest.mark.asyncio
async def test_unknown_admission_does_not_allow_a_second_run(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    create = fake.create_run
    async def lost_receipt(request):
        await create(request)
        return web.json_response({'error': 'response lost after admission'}, status=503)
    fake.create_run = lost_receipt
    async with Harness(fake, device_store=DeviceStore(tmp_path/'devices.db')) as client:
        first, _ = await connect(client)
        for chunk in _speech_then_silence_pcm():
            await first.send_bytes(chunk)
        await _collect_turn(first)
        second, error = await connect(client)
        assert error['error'] == 'conversation_busy' and len(fake.runs) == 1
        await second.close()


def test_prompt_explains_names_proactivity_and_real_scheduling_receipts():
    from hermes_converse.loop import voice_system_prompt
    prompt = voice_system_prompt('Alexa', device_name='Den Echo', recipient_id='opaque')
    assert 'Your name is Alexa' in prompt and 'Your name is Den Echo' not in prompt
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
        assert len(fake.sessions) == 1
        assert len({r['extensions']['arya-v1']['recipient_id'] for _, r in results}) == 1
        assert len(harness.route.hub._attachments) == 1
        for ws, _ in results:
            await ws.close()


@pytest.mark.asyncio
async def test_compression_title_handoff_and_hidden_root_do_not_lose_history(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    store = DeviceStore(tmp_path/'devices.db')
    device = store.reserve('Alexa', 'Den Echo')
    store.remember(device, 'root')
    fake.sessions['root'] = {'id': 'root', 'source': 'api_server', 'title': 'Alexa: Den Echo',
                             'end_reason': 'compression', 'hidden': True, 'archived': True}
    fake.sessions['tip'] = {'id': 'tip', 'source': 'api_server', 'title': None, 'parent_session_id': 'root'}
    fake.continuations['root'] = 'tip'
    async with Harness(fake, device_store=store) as client:
        ws, ready = await connect(client)
        assert ready['type'] == 'ready'
        await _audio_turn(ws)
        assert next(iter(fake.runs.values()))['body']['session_id'] == 'root'
        await ws.close()


@pytest.mark.asyncio
async def test_title_lookup_adopts_lineage_root_from_projected_tip(tmp_path, monkeypatch):
    fake = _patch(monkeypatch)
    fake.sessions['root'] = {'id': 'root', 'source': 'api_server', 'title': None, 'end_reason': 'compression'}
    fake.sessions['tip'] = {'id': 'tip', 'source': 'api_server', 'title': 'Alexa: Den Echo',
                            '_lineage_root_id': 'root'}
    fake.continuations['root'] = 'tip'
    store = DeviceStore(tmp_path/'devices.db')
    async with Harness(fake, device_store=store) as client:
        ws, ready = await connect(client)
        assert ready['type'] == 'ready'
        saved = store.reserve('Alexa', 'Den Echo')
        assert saved.session_id == 'root'
        await _audio_turn(ws)
        assert next(iter(fake.runs.values()))['body']['session_id'] == 'root'
        await ws.close()
