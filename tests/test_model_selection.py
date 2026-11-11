"""Model changes affect only subsequent runs on the authenticated voice socket."""
import pytest
from hermes_converse.model_selection import parse_model_selection


@pytest.mark.parametrize('frame', [
    {'model': ''}, {'model': []}, {'model': 'x\ny'}, {'model': 'x', 'provider': 4},
    {'model': None, 'provider': 'litellm'}, {'model': 'x' * 257},
])
def test_invalid_selection(frame):
    with pytest.raises(ValueError):
        parse_model_selection(frame)


def test_default_and_provider_choice():
    assert parse_model_selection({'model': None, 'provider': None}) == (None, None)
    assert parse_model_selection({'model': ' beta ', 'provider': ' litellm '}) == ('beta', 'litellm')


@pytest.mark.parametrize('named', [False, True])
async def test_switch_does_not_interrupt_current_run_and_reset_restores_default(monkeypatch, named):
    from hermes_converse import route as route_module
    from tests.test_route import API_KEY, Harness, _FakeStreamer, _patch_speech, _collect_turn
    from tests.support.fake_api import FakeRunsAPI
    import numpy as np

    fake = FakeRunsAPI(API_KEY, deltas=('Four.',), hang=True)
    _patch_speech(monkeypatch, _FakeStreamer([np.zeros(240, np.int16).tobytes()]))
    created = []
    original = route_module._resolve_session

    def capture_session(*args):
        result = original(*args)
        created.append(result[-1])
        return result

    monkeypatch.setattr(route_module, '_resolve_session', capture_session)
    async with Harness(fake, {'voice_model': 'agent', 'voice_provider': 'litellm'}) as client:
        ws = await client.ws_connect('/v1/audio/converse')
        await ws.send_json({'type': 'start', 'key': API_KEY,
                            **({'assistant_name': 'Arya', 'device_name': 'Office Mac'} if named else {})})
        assert (await ws.receive_json())['type'] == 'ready'
        session = created[0]
        session.transcripts.put('first question')
        while (await ws.receive_json())['type'] != 'thinking':
            pass
        # thinking can precede Runs admission by one event-loop tick.
        import asyncio
        for _ in range(100):
            if fake.runs:
                break
            await asyncio.sleep(.01)
        assert next(iter(fake.runs.values()))['body']['model'] == 'agent'
        await ws.send_json({'type': 'set_model', 'request_id': 'bad-choice', 'model': []})
        assert await ws.receive_json() == {'type': 'model_error', 'request_id': 'bad-choice',
                                          'error': 'bad_model_selection'}
        await ws.send_json({'type': 'set_model', 'model': 'beta'})
        assert await ws.receive_json() == {'type': 'model_error', 'error': 'bad_request_id'}
        await ws.send_json({'type': 'set_model', 'request_id': 'choose-beta', 'model': 'beta', 'provider': 'litellm'})
        changed = await ws.receive_json()
        assert changed == {'type': 'model_changed', 'request_id': 'choose-beta', 'model': 'beta',
                           'provider': 'litellm', 'uses_default': False, 'applies_to': 'next_turn'}
        assert fake.stop_calls == [], 'changing a model must not cancel the current run'
        assert len(fake.runs) == 1 and next(iter(fake.runs.values()))['body']['model'] == 'agent'
        await ws.send_json({'type': 'cancel'})
        await _collect_turn(ws)
        fake.hang = False
        session.transcripts.put('second question')
        await _collect_turn(ws)
        bodies = [run['body'] for run in fake.runs.values()]
        assert bodies[-1]['model'] == 'beta' and bodies[-1]['provider'] == 'litellm'
        await ws.send_json({'type': 'set_model', 'request_id': 'reset', 'model': None, 'provider': None})
        reset = await ws.receive_json()
        assert reset['uses_default'] and reset['model'] == 'agent'
        session.transcripts.put('third question')
        await _collect_turn(ws)
        assert list(fake.runs.values())[-1]['body']['model'] == 'agent'
        assert len({run['body']['session_id'] for run in fake.runs.values()}) == 1
        await ws.close()


@pytest.mark.parametrize('unavailable', [False, True])
@pytest.mark.parametrize('named', [False, True])
@pytest.mark.parametrize('models', [None, False, True, 'true', 1])
async def test_ready_catalog_is_opt_in(monkeypatch, tmp_path, unavailable, named, models):
    import numpy as np
    from aiohttp import web
    from tests.test_route import API_KEY, Harness, _FakeStreamer, _patch_speech
    from tests.support.fake_api import FakeRunsAPI

    fake = FakeRunsAPI(API_KEY)
    _patch_speech(monkeypatch, _FakeStreamer([np.zeros(240, np.int16).tobytes()]))
    queries = []

    async def inventory(request):
        assert request.headers['Authorization'] == f'Bearer {API_KEY}'
        queries.append(dict(request.query))
        if unavailable:
            return web.json_response({}, status=503)
        return web.json_response({'model': 'agent', 'provider': 'litellm', 'api_key': 'secret',
            'providers': [{'slug': 'litellm', 'name': 'LiteLLM', 'models': ['agent', 'beta', 'beta'],
                           'authenticated': True, 'api_key': 'secret'},
                          {'slug': 'unconfigured', 'authenticated': False, 'models': ['hidden']}]})

    fake.model_options = inventory
    from hermes_converse.named import DeviceStore
    async with Harness(fake, device_store=DeviceStore(tmp_path / "devices.db")) as client:
        ws = await client.ws_connect('/v1/audio/converse')
        start = {'type': 'start', 'key': API_KEY}
        if models is not None:
            start['models'] = models
        if named:
            start.update(assistant_name='Arya', device_name='Office Mac')
        await ws.send_json(start)
        ready = await ws.receive_json()
        assert ready['type'] == 'ready'
        if models is not True:
            assert 'model_catalog' not in ready
            assert queries == []
        else:
            assert ready['model_catalog'] == ({'providers': []} if unavailable else {
                'model': 'agent', 'provider': 'litellm',
                'providers': [{'slug': 'litellm', 'name': 'LiteLLM', 'models': ['agent', 'beta']}]})
            assert queries == [{'include_unconfigured': 'false', 'refresh': 'false'}]
        await ws.send_json({'type': 'set_model', 'request_id': 'pick', 'model': 'beta', 'provider': 'litellm'})
        assert (await ws.receive_json())['type'] == 'model_changed'
        assert len(queries) == (1 if models is True else 0)
        await ws.close()
