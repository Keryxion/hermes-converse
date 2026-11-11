"""Test bootstrap: an isolated Hermes home, the Hermes checkout on the path, and the plugin
directory importable as the ``hermes_converse`` package (the loader imports it the same way,
under ``hermes_plugins.hermes_converse``)."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Before any Hermes module is imported: they resolve HERMES_HOME at import time.
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="hermes-converse-tests-")

# The Hermes checkout: HERMES_AGENT_ROOT when Hermes is not already importable.
_agent_root = os.environ.get("HERMES_AGENT_ROOT")
if _agent_root and _agent_root not in sys.path:
    sys.path.insert(0, _agent_root)


def _load_plugin_package() -> None:
    if "hermes_converse" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "hermes_converse", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_converse"] = module
    spec.loader.exec_module(module)


_load_plugin_package()


# Most audio fixtures are modulated noise for the energy detector. Keep ordinary tests
# offline and exercise the missing-model fallback; Silero-specific tests inject a session.
import pytest


@pytest.fixture(autouse=True)
def _offline_silero(monkeypatch):
    monkeypatch.setattr('hermes_converse.speech_detector.load_silero_session',
                        lambda: (None, 'model omitted in offline tests'))
    monkeypatch.setattr('hermes_converse.silero_vad.load_silero_session',
                        lambda: (None, 'model omitted in offline tests'))
