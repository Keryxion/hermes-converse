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


async def test_preroll_burst_keeps_audio_and_following_turn_events(tmp_path, monkeypatch):
    import json
    import wave
    from hermes_converse import _server_recording as recording
    from tests.test_recording_privacy import configure, session
    configure(monkeypatch, tmp_path)
    r = recording.ServerRecording(session())
    for _ in range(200):
        r.audio(b'\x01\x00' * 320)
    r.diagnostic({'type': 'stt_completed', 'text': 'test request'})
    r.frame({'type': 'turn_done', 'turn_id': 'test'})
    r.close()
    await r.wait_closed()
    path = next(p for p in recording.ROOT.glob('*.wav') if not p.stem.endswith('-output'))
    with wave.open(str(path)) as wav:
        assert wav.getnframes() == 200 * 320
    events = [json.loads(line) for line in next(recording.ROOT.glob('*.jsonl')).read_text().splitlines()]
    assert events[-2]['frame']['type'] == 'stt_completed'
    assert events[-1]['frame']['type'] == 'turn_done'


async def test_overflow_records_why_capture_stopped(tmp_path, monkeypatch):
    import json
    from hermes_converse import _server_recording as recording
    from tests.test_recording_privacy import configure, session
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(recording, 'QUEUE_SIZE', 2)
    r = recording.ServerRecording(session())
    for _ in range(3):
        r.audio(bytes(640))
    await r.wait_closed()
    events = [json.loads(line) for line in next(recording.ROOT.glob('*.jsonl')).read_text().splitlines()]
    assert events[-1]['event'] == 'recording_stopped'
    assert events[-1]['reason'] == 'queue_full'


async def test_capture_diagnostics_distinguish_stt_from_no_data(tmp_path, monkeypatch):
    for no_data in (False, True):
        path = tmp_path / 'capture.wav'
        path.write_bytes(b'synthetic')
        called = []
        s = ConverseSession(np, transcribe=lambda path: called.append(path) or 'test request')
        frames = []
        monkeypatch.setattr(s, '_record_diagnostic', frames.append)
        async def capture(**kwargs):
            s.stream.no_data_return = no_data
            return str(path), 1
        monkeypatch.setattr(s, '_capture', capture)
        text, _ = await s._capture_and_transcribe(wake=True)
        if no_data:
            assert not called and text == ''
            assert [f['type'] for f in frames] == ['capture_started', 'stt_skipped']
            assert frames[-1]['reason'] == 'no_audio_data'
        else:
            assert len(called) == 1 and text == 'test request'
            assert [f['type'] for f in frames] == ['capture_started', 'stt_started', 'stt_completed']
            assert frames[-1]['text_chars'] == 12
        assert not path.exists()
