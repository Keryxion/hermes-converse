"""Voice-only compaction uses the real compressor without a provider or sockets."""
from types import SimpleNamespace

import pytest

from agent.context_compressor import ContextCompressor
from hermes_converse.compaction import install_voice_compaction
from hermes_converse.settings import load_settings


def adapter_and_hub(existing_cap=None):
    def create_agent(*, session_id):
        compressor = ContextCompressor('test-model', config_context_length=524288,
            quiet_mode=True, threshold_tokens_cap=existing_cap)
        return SimpleNamespace(session_id=session_id, context_compressor=compressor,
                               compression_enabled=True)

    adapter = SimpleNamespace(_create_agent=create_agent)
    hub = SimpleNamespace(voice_target=lambda sid: 'voice-root' if sid in {'voice-root', 'voice-tip'} else None)
    return adapter, hub


def test_voice_cap_triggers_at_64k_and_survives_model_switch():
    adapter, hub = adapter_and_hub()
    original = adapter._create_agent
    uninstall = install_voice_compaction(adapter, hub, {'compression_threshold_tokens':64000}.get)
    voice = adapter._create_agent(session_id='voice-tip')
    other = adapter._create_agent(session_id='ordinary-api')
    cc = voice.context_compressor
    assert cc.threshold_tokens == 64000
    assert not cc.should_compress(prompt_tokens=63999)
    assert cc.should_compress(prompt_tokens=64000)
    assert other.context_compressor.threshold_tokens > 64000
    assert other.context_compressor.threshold_tokens_cap is None
    cc.update_model('other-model', 1048576)
    assert cc.threshold_tokens == 64000
    uninstall()
    assert adapter._create_agent is original


@pytest.mark.parametrize('setting,existing,expected', [(0,None,None), (64000,32000,32000)])
def test_disabled_setting_and_existing_stricter_limit(setting, existing, expected):
    adapter, hub = adapter_and_hub(existing)
    install_voice_compaction(adapter, hub, {'compression_threshold_tokens':setting}.get)
    voice = adapter._create_agent(session_id='voice-root')
    assert voice.context_compressor.threshold_tokens_cap == expected


def test_setting_and_platform_factory_installation():
    from aiohttp import web
    from hermes_converse.route import make_platform_factory
    settings = {'compression_threshold_tokens':64000}
    assert load_settings(settings.get).compression_threshold_tokens == 64000
    adapter, hub = adapter_and_hub()
    cleanups = []
    ctx = SimpleNamespace(get_config=settings.get, profile_name='default', on_unload=cleanups.append)
    make_platform_factory(ctx, hub=hub)(web.Application(), adapter)
    assert adapter._create_agent(session_id='voice-root').context_compressor.threshold_tokens == 64000
    assert adapter._create_agent(session_id='ordinary-api').context_compressor.threshold_tokens_cap is None
    for cleanup in cleanups:
        cleanup()
