"""Opt-in interaction recording."""

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


async def test_complete_interaction_past_one_minute(tmp_path, monkeypatch):
    import json
    import wave
    from hermes_converse import _server_recording as recording
    monkeypatch.setattr(recording, 'ROOT', tmp_path / 'recordings')
    monkeypatch.setattr(recording, '_mute_disarmed', False)
    monkeypatch.setenv('HERMES_VOICE_DEBUG', '1')
    monkeypatch.setenv('HERMES_CONVERSE_SERVER_RECORDING', '1')
    s = ConverseSession(np)
    files = recording._Files(s)
    assert files.open()
    try:
        for _ in range(61):
            assert files.write(recording._Audio(bytes(s._input_rate * 2)))
        assert files.write((b'\x01\x00' * 2400, 1234))
        assert files.write({'event': 'client', 'frame': {'type': 'text', 'text': 'hello'}})
    finally:
        files.close()
    paths = list(recording.ROOT.glob('*.wav'))
    with wave.open(str(next(p for p in paths if not p.stem.endswith('-output')))) as wav:
        assert wav.getnframes() == 61 * s._input_rate
    with wave.open(str(next(p for p in paths if p.stem.endswith('-output')))) as wav:
        assert wav.getnframes() == 2400
        assert wav.getframerate() == 24000
    events = [json.loads(line) for line in next(recording.ROOT.glob('*.jsonl')).read_text().splitlines()]
    assert events[-1]['frame']['text'] == 'hello'
    audio = [e for e in events if e['event'] == 'audio']
    assert audio[-2]['offset_samples'] == 60 * s._input_rate
    assert audio[-1]['direction'] == 'output'
    assert audio[-1]['received_unix_ms'] == 1234


def test_live_wav_headers_cover_all_written_pcm(tmp_path, monkeypatch):
    import wave
    from hermes_converse import _server_recording as recording
    monkeypatch.setattr(recording, 'ROOT', tmp_path / 'recordings')
    monkeypatch.setenv('HERMES_VOICE_DEBUG', '1')
    monkeypatch.setenv('HERMES_CONVERSE_SERVER_RECORDING', '1')
    s = type('Session', (), {'_input_rate': 16000, 'session_id': 'test',
                            '_detector': type('Detector', (), {'name': 'test'})()})()
    files = recording._Files(s)
    assert files.open()
    try:
        for _ in range(2):
            assert files.write(recording._Audio(bytes(3200)))
            assert files.write((bytes(4800), 1234))
        for path in recording.ROOT.glob('*.wav'):
            with wave.open(str(path)) as wav:
                expected = 4800 if path.stem.endswith('-output') else 3200
                assert wav.getnframes() == expected
                assert len(wav.readframes(expected)) == expected * 2
    finally:
        files.close()
