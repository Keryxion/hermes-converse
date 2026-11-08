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


async def test_catalog_uses_socket_and_does_not_block_audio_or_model_changes(monkeypatch):
    import asyncio
    import numpy as np
    from aiohttp import web
    from hermes_converse import route as route_module
    from tests.test_route import API_KEY, Harness, _FakeStreamer, _patch_speech
    from tests.support.fake_api import FakeRunsAPI

    fake = FakeRunsAPI(API_KEY)
    _patch_speech(monkeypatch, _FakeStreamer([np.zeros(240, np.int16).tobytes()]))
    entered, release = asyncio.Event(), asyncio.Event()
    queries, received = [], []

    async def inventory(request):
        assert request.headers['Authorization'] == f'Bearer {API_KEY}'
        queries.append(dict(request.query))
        if request.query.get('refresh') == 'true':
            return web.json_response({'error': 'unavailable'}, status=503)
        entered.set()
        await release.wait()
        return web.json_response({'model': 'agent', 'provider': 'litellm', 'api_key': 'never-forward',
            'providers': [{'slug': 'litellm', 'name': 'LiteLLM', 'models': ['agent', 'beta', 'beta'],
                           'authenticated': True, 'api_key': 'never-forward'},
                          {'slug': 'unconfigured', 'authenticated': False, 'models': ['hidden']}]})

    original_routes = fake.routes
    fake.routes = lambda: [*original_routes(), web.get('/api/model/options', inventory)]
    original_session = route_module._resolve_session

    def capture_input(*args):
        result = original_session(*args)
        feed = result[-1].stream.feed
        def observed(data):
            received.append(data)
            feed(data)
        result[-1].stream.feed = observed
        return result

    monkeypatch.setattr(route_module, '_resolve_session', capture_input)
    async with Harness(fake) as client:
        ws = await client.ws_connect('/v1/audio/converse')
        await ws.send_json({'type': 'start', 'key': API_KEY})
        assert (await ws.receive_json())['type'] == 'ready'
        await ws.send_json({'type': 'list_models', 'request_id': 'catalog'})
        await asyncio.wait_for(entered.wait(), 2)
        pcm = b'\0' * 960
        await ws.send_bytes(pcm)
        await ws.send_json({'type': 'set_model', 'request_id': 'pick', 'model': 'beta', 'provider': 'litellm'})
        assert (await asyncio.wait_for(ws.receive_json(), 2))['type'] == 'model_changed'
        assert pcm in received, 'slow discovery must not hold microphone frames'
        await ws.send_json({'type': 'list_models', 'request_id': 'duplicate'})
        assert (await ws.receive_json())['error'] == 'model_catalog_busy'
        release.set()
        catalog = await ws.receive_json()
        assert catalog == {'type': 'model_list', 'request_id': 'catalog', 'model': 'agent', 'provider': 'litellm',
            'providers': [{'slug': 'litellm', 'name': 'LiteLLM', 'models': ['agent', 'beta']}]}
        assert 'never-forward' not in str(catalog)
        assert queries[0]['include_unconfigured'] == 'false'
        await ws.send_json({'type': 'list_models', 'request_id': 'bad-refresh', 'refresh': 'false'})
        assert (await ws.receive_json())['error'] == 'bad_model_request'
        await ws.send_json({'type': 'list_models', 'request_id': 'offline', 'refresh': True})
        failure = await ws.receive_json()
        assert failure == {'type': 'model_error', 'operation': 'list_models',
                           'request_id': 'offline', 'error': 'model_catalog_unavailable'}
        await ws.send_json({'type': 'set_model', 'request_id': 'reset', 'model': None, 'provider': None})
        assert (await ws.receive_json())['type'] == 'model_changed'
        await ws.close()


async def test_disconnect_cancels_pending_catalog_lookup(monkeypatch):
    import asyncio
    import numpy as np
    from hermes_converse.agent_client import RunsClient
    from tests.test_route import API_KEY, Harness, _FakeStreamer, _patch_speech
    from tests.support.fake_api import FakeRunsAPI

    _patch_speech(monkeypatch, _FakeStreamer([np.zeros(240, np.int16).tobytes()]))
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def slow_catalog(self, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(RunsClient, 'model_options', slow_catalog)
    async with Harness(FakeRunsAPI(API_KEY)) as client:
        ws = await client.ws_connect('/v1/audio/converse')
        await ws.send_json({'type': 'start', 'key': API_KEY})
        await ws.receive_json()
        await ws.send_json({'type': 'list_models', 'request_id': 'pending'})
        await asyncio.wait_for(entered.wait(), 2)
        await ws.close()
        await asyncio.wait_for(cancelled.wait(), 2)
