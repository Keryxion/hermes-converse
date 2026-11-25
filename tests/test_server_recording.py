"""The standard installation routes diagnostics to a no-op."""
import sys
import time

import numpy as np

from hermes_converse import route
from hermes_converse.loop import ConverseSession


async def test_normal_install_ignores_runtime_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv('CONVERSE_DIAGNOSTIC_RECORDING', '1')
    monkeypatch.setenv('CONVERSE_DIAGNOSTIC_UNTIL', str(time.time() + 60))
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delitem(sys.modules, 'hermes_converse._server_recording', raising=False)
    r = route.diagnostic.ServerRecording(None)
    r.audio(b'\x01\x00')
    r.frame({'type': 'turn_done'})
    r.close()
    await r.wait_closed()
    assert 'hermes_converse._server_recording' not in sys.modules
    assert not list(tmp_path.iterdir())


def test_recording_uses_session_mute_at_feed(monkeypatch):
    session = ConverseSession(np)
    accepted = []
    feed = session.stream.feed

    def feed_under_lock(pcm):
        assert session._state_lock._is_owned()
        feed(pcm)

    def record_under_lock(pcm):
        assert session._state_lock._is_owned()
        accepted.append(pcm)

    monkeypatch.setattr(session.stream, 'feed', feed_under_lock)
    session.ignore_microphone(muted=True)
    session.feed_audio(b'muted', record_under_lock)
    session.unmute()
    session.feed_audio(b'live', record_under_lock)
    session.ignore_microphone(muted=False)
    session.feed_audio(b'held', record_under_lock)
    assert accepted == [b'', b'live', b'held']
