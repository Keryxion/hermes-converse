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


@pytest.mark.parametrize('unavailable', [False, True])
@pytest.mark.parametrize('models', [None, False, True, 'true', 1])
async def test_ready_catalog_is_opt_in(monkeypatch, tmp_path, unavailable, models):
    import numpy as np
    from aiohttp import web
    from tests.test_route import API_KEY, Harness, _FakeStreamer, _patch_speech
    from tests.support.fake_hermes import FakeHermes

    fake = FakeHermes(API_KEY)
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
        start = {'type': 'start', 'key': API_KEY, 'assistant_name': 'Arya', 'device_name': 'Office Mac'}
        if models is not None:
            start['models'] = models
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
        await ws.send_json({'type': 'command', 'name': 'model', 'args': ['beta', '--provider', 'litellm', '--session']})
        assert (await ws.receive_json())['current_model']['model'] == 'beta'
        assert len(queries) == (1 if models is True else 0)
        await ws.close()
