"""Dependency metadata must parse and preserve the supported-platform fallback."""
from pathlib import Path

from packaging.requirements import Requirement
import pytest
import yaml


@pytest.fixture(scope='module')
def requirements():
    manifest = yaml.safe_load((Path(__file__).resolve().parents[1] / 'plugin.yaml').read_text())
    return {requirement.name: requirement for requirement in
            map(Requirement, manifest['python_dependencies'])}


@pytest.mark.parametrize('system,machine,include_onnx', [
    ('linux', 'x86_64', True),
    ('linux', 'aarch64', True),
    ('darwin', 'arm64', True),
    ('darwin', 'x86_64', False),
    ('android', 'aarch64', False),
    ('android', 'x86_64', False),
    ('win32', 'AMD64', True),
])
def test_smart_turn_dependency_platforms(requirements, system, machine, include_onnx):
    environment = {'sys_platform': system, 'platform_machine': machine}
    assert requirements['onnxruntime'].marker.evaluate(environment) is include_onnx
    # The feature extractor and model downloader remain available on every platform.
    for name in ('transformers', 'huggingface_hub'):
        marker = requirements[name].marker
        assert marker is None or marker.evaluate(environment)
