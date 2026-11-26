"""The standard installation routes diagnostics to a no-op."""

import numpy as np

from hermes_converse import route
from hermes_converse.loop import ConverseSession


async def test_recording_requires_both_debug_flags(tmp_path, monkeypatch):
    from hermes_converse import _server_recording as recording
    monkeypatch.setattr(recording, 'ROOT', tmp_path / 'recordings')
    monkeypatch.setattr(recording, '_mute_disarmed', False)
    for voice, server, expected in [('', '', False), ('1', '', False),
                                     ('', '1', False), ('0', '1', False),
                                     ('1', '0', False), ('1', '1', True)]:
        monkeypatch.setenv('HERMES_VOICE_DEBUG', voice)
        monkeypatch.setenv('HERMES_CONVERSE_SERVER_RECORDING', server)
        assert recording._enabled() is expected
        r = route.diagnostic.ServerRecording(ConverseSession(np))
        assert (r._task is not None) is expected
        r.close()
        await r.wait_closed()
    assert not (tmp_path / 'recordings').exists() or all(
        p.suffix in ('.wav', '.jsonl') for p in (tmp_path / 'recordings').iterdir())


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
