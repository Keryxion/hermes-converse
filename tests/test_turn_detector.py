"""Smart Turn load failures leave a usable fixed endpoint without repeated attempts."""
import sys
from types import ModuleType

import numpy as np
import pytest

from hermes_converse import turn_detector
from hermes_converse.loop import ConverseSession


@pytest.mark.parametrize('failure', ['missing_dependency', 'weights_unavailable'])
def test_unavailable_smart_turn_keeps_fixed_endpoint(monkeypatch, caplog, failure):
    monkeypatch.setattr(turn_detector, '_cached', None)
    monkeypatch.setattr(turn_detector, '_load_attempted', False)
    if failure == 'missing_dependency':
        monkeypatch.setitem(sys.modules, 'onnxruntime', None)
    else:
        monkeypatch.setitem(sys.modules, 'onnxruntime', ModuleType('onnxruntime'))
        hub = ModuleType('huggingface_hub')
        def unavailable(*args):
            raise OSError('offline with an empty cache')
        hub.hf_hub_download = unavailable
        monkeypatch.setitem(sys.modules, 'huggingface_hub', hub)
        transformers = ModuleType('transformers')
        transformers.WhisperFeatureExtractor = object
        monkeypatch.setitem(sys.modules, 'transformers', transformers)

    session = ConverseSession(np, endpoint_model=True)
    assert session._turn_detector is None
    assert turn_detector.load_turn_detector() is None
    messages = [r.message for r in caplog.records if 'smart-turn endpointer unavailable' in r.message]
    assert len(messages) == 1
    assert 'using the fixed silence endpoint' in messages[0]
