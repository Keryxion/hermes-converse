"""Doctor reports the effective endpointer and an actionable recovery path."""
from types import ModuleType, SimpleNamespace
import sys

import pytest

from hermes_converse import cli, turn_detector


@pytest.fixture
def doctor_config(monkeypatch):
    cfg = {'plugins': {'entries': {'hermes-converse': {'settings': {
        'endpoint_model': 'smart-turn-v3', 'endpoint_threshold': 0.7, 'vad_model': 'energy',
    }}}}}

    def module(name, **attributes):
        value = ModuleType(name)
        value.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, value)

    for parent in ('hermes_cli', 'gateway', 'agent'):
        module(parent, __path__=[])
    module('hermes_cli.config', load_config=lambda: cfg)
    module('gateway.config', Platform=SimpleNamespace(API_SERVER='api_server'),
           load_gateway_config=lambda: SimpleNamespace(platforms={
               'api_server': SimpleNamespace(enabled=True, extra={}),
           }))
    module('agent.secret_scope', get_secret=lambda *args: 'test-key')
    monkeypatch.setattr(cli.importlib.util, 'find_spec', lambda name: object())
    return cfg['plugins']['entries']['hermes-converse']['settings']


def test_disabled_smart_turn_does_not_load_or_check_model(doctor_config, monkeypatch, capsys):
    doctor_config['endpoint_model'] = ''

    def unexpected(*args):
        pytest.fail('Smart Turn must stay lazy when disabled')

    monkeypatch.setattr(turn_detector, 'missing_modules', unexpected)
    monkeypatch.setattr(turn_detector, 'load_turn_detector', unexpected)
    assert cli.doctor() == 0
    assert 'fixed silence' in capsys.readouterr().out


def test_missing_dependencies_report_install_and_fallback(doctor_config, monkeypatch, capsys):
    monkeypatch.setattr(turn_detector, 'missing_modules', lambda: ['onnxruntime', 'transformers'])
    monkeypatch.setattr(turn_detector, 'load_turn_detector', lambda *a: pytest.fail('missing deps'))
    assert cli.doctor() == 1
    output = capsys.readouterr().out
    assert 'missing: onnxruntime, transformers' in output
    assert 'plugin manager' in output and 'restart the gateway' in output
    assert 'using fixed silence' in output


@pytest.mark.parametrize('loaded', [True, False])
def test_doctor_checks_real_model_readiness(doctor_config, monkeypatch, capsys, loaded):
    monkeypatch.setattr(turn_detector, 'missing_modules', lambda: [])
    thresholds = []

    def load(threshold):
        thresholds.append(threshold)
        return object() if loaded else None

    monkeypatch.setattr(turn_detector, 'load_turn_detector', load)
    assert cli.doctor() == (0 if loaded else 1)
    assert thresholds == [0.7]
    output = capsys.readouterr().out
    if loaded:
        assert 'enabled (threshold 0.7)' in output
    else:
        assert 'model could not load; using fixed silence' in output
        assert 'cache' in output


@pytest.mark.parametrize('loaded', [True, False])
def test_doctor_reports_silero_and_energy_fallback(doctor_config, monkeypatch, capsys, loaded):
    doctor_config.update(vad_model='silero', endpoint_model='')
    monkeypatch.setattr('hermes_converse.silero_vad.load_silero_session',
                        lambda: (object(), None) if loaded else (None, 'weights unavailable'))
    assert cli.doctor() == (0 if loaded else 1)
    output = capsys.readouterr().out
    assert ('Silero (CPU)' if loaded else 'using energy') in output


def test_energy_doctor_does_not_load_silero(doctor_config, monkeypatch):
    doctor_config.update(vad_model='energy', endpoint_model='')
    monkeypatch.setattr('hermes_converse.silero_vad.load_silero_session',
                        lambda: pytest.fail('energy must not load the model'))
    assert cli.doctor() == 0
