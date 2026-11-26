"""Synthetic-only privacy and retention checks for the debug implementation."""
import asyncio
import fcntl
import os
import subprocess
import sys
import time
from types import SimpleNamespace
import wave

from hermes_converse import _server_recording as recording


def session():
    return SimpleNamespace(_input_rate=16000, session_id='synthetic',
                           _detector=SimpleNamespace(name='test'))


def configure(monkeypatch, tmp_path):
    monkeypatch.setattr(recording, '_mute_disarmed', False)
    monkeypatch.setattr(recording, 'ROOT', tmp_path / 'recordings')
    monkeypatch.setenv('HERMES_VOICE_DEBUG', '1')
    monkeypatch.setenv('HERMES_CONVERSE_SERVER_RECORDING', '1')


async def test_disabled_gate_stops_existing_stream_and_rms_timeline(tmp_path, monkeypatch):
    configure(monkeypatch, tmp_path)
    written = asyncio.Event()
    loop = asyncio.get_running_loop()
    write = recording._Files.write

    def notify_after_write(files, item):
        result = write(files, item)
        loop.call_soon_threadsafe(written.set)
        return result

    monkeypatch.setattr(recording._Files, 'write', notify_after_write)
    r = recording.ServerRecording(session())
    r.audio((1000).to_bytes(2, 'little', signed=True) * 1600)
    await asyncio.wait_for(written.wait(), 3)
    monkeypatch.setenv('HERMES_VOICE_DEBUG', '0')
    r.audio(b'\x00\x00' * 1600)
    await r.wait_closed()
    with wave.open(str(next(recording.ROOT.glob('*.wav'))), 'rb') as wav:
        assert wav.getnframes() == 1600
    assert '"rms": 1000.0' in next(recording.ROOT.glob('*.jsonl')).read_text()
    assert recording.ROOT.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in recording.ROOT.iterdir())


async def test_idle_recording_closes_at_retention_limit(tmp_path, monkeypatch):
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(recording, 'MAX_AGE_SECONDS', .1)
    r = recording.ServerRecording(session())
    await asyncio.wait_for(r.wait_closed(), 2)
    assert r._queue is None


def test_maintenance_removes_old_pairs_orphans_and_legacy(tmp_path, monkeypatch):
    configure(monkeypatch, tmp_path)
    root = recording.ROOT
    root.mkdir()
    old = time.time() - recording.MAX_AGE_SECONDS - 1
    for name in ('input-pair.wav', 'input-pair.jsonl', 'input-orphan.jsonl',
                 'capture-old.wav', 'capture-old.json', 'capture-orphan.json',
                 'input-active.wav', 'unrelated.wav'):
        path = root / name
        path.touch()
        os.utime(path, (old, old))
    (root / 'input-fresh.jsonl').touch()
    with (root / 'input-active.wav').open('rb') as active:
        fcntl.flock(active, fcntl.LOCK_EX | fcntl.LOCK_NB)
        recording._cleanup()
        assert {p.name for p in root.iterdir()} == {
            'input-active.wav', 'input-fresh.jsonl', 'unrelated.wav',
        }
    recording._cleanup()
    assert not (root / 'input-active.wav').exists()


def test_storage_is_bounded_across_processes(tmp_path, monkeypatch):
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(recording, 'MAX_RECORDINGS', 1)
    files = recording._Files(session())
    original_open = recording._open_private
    child = """
import runpy, sys, time
from pathlib import Path
from types import SimpleNamespace
m = runpy.run_path(sys.argv[1])
state = m['_enabled'].__globals__
state.update(ROOT=Path(sys.argv[2]), MAX_RECORDINGS=1)
files = m['_Files'](SimpleNamespace(_input_rate=16000, session_id='child',
                                  _detector=SimpleNamespace(name='test')))
try:
    assert not files.open()
finally:
    files.close()
"""
    command = [sys.executable, '-c', child, recording.__file__, str(recording.ROOT)]

    def check_during_reservation(*args):
        monkeypatch.setattr(recording, '_open_private', original_open)
        subprocess.run(command, check=True, timeout=5)
        return original_open(*args)

    monkeypatch.setattr(recording, '_open_private', check_during_reservation)
    try:
        assert files.open()
    finally:
        files.close()
    subprocess.run(command, check=True, timeout=5)
    assert len(list(recording.ROOT.iterdir())) == 2


def test_close_releases_other_handles_when_wav_close_fails():
    def fail():
        raise OSError('disk write failed')
    closed = []
    files = recording._Files(session())
    files.wav = SimpleNamespace(close=fail)
    files.events = SimpleNamespace(close=lambda: closed.append('events'))
    files._wav_file = SimpleNamespace(close=lambda: closed.append('file'))
    files.close()
    files.close()
    assert closed == ['events', 'file']


async def test_mute_boundary_does_not_join_samples(tmp_path, monkeypatch):
    configure(monkeypatch, tmp_path)
    r = recording.ServerRecording(session())
    for packet in (b'A', b'', b'BC', b'D', b'E'):
        r.audio(packet)
    r.close()
    await r.wait_closed()
    with wave.open(str(next(recording.ROOT.glob('*.wav'))), 'rb') as wav:
        assert wav.readframes(10) == b'BCDE'


def test_large_write_stops_when_disabled_between_chunks(monkeypatch):
    files = recording._Files(session())
    written = []
    active = [True]
    def write(data):
        written.append(data)
        active[0] = False
    files.wav = SimpleNamespace(writeframesraw=write)
    monkeypatch.setattr(recording, '_enabled', lambda: active[0])
    assert not files.write(b'\x01\x00' * 16000)
    assert 0 < len(written[0]) < 32000
    assert len(written) == 1


async def test_muted_resampler_tail_never_reaches_recorded_wav(tmp_path, monkeypatch):
    import numpy as np
    from hermes_converse.audio_transport import AudioTransport
    from hermes_converse.loop import ConverseSession

    configure(monkeypatch, tmp_path)
    s = ConverseSession(np)
    transport = AudioTransport('pcm16', 48000, 16000)
    r = recording.ServerRecording(s)
    s.ignore_microphone(muted=True)
    s.feed_encoded_audio(transport, (30000).to_bytes(2, 'little', signed=True) * 480, r.audio)
    s.unmute()
    s.feed_encoded_audio(transport, bytes(960), r.audio)
    r.close()
    await r.wait_closed()
    for path in recording.ROOT.glob('*.wav'):
        with wave.open(str(path), 'rb') as wav:
            assert wav.getnframes() == 0


async def test_muted_opus_decoder_history_never_reaches_recorded_wav(tmp_path, monkeypatch):
    import numpy as np
    import pytest
    from hermes_converse.audio_transport import AudioTransport
    from hermes_converse.loop import ConverseSession
    from hermes_converse.opus import Decoder, Encoder

    configure(monkeypatch, tmp_path)
    try:
        transport = AudioTransport('opus', 16000, 16000)
        muted_encoder, live_encoder = Encoder(16000), Encoder(16000)
    except OSError as exc:
        pytest.skip(str(exc))
    s = ConverseSession(np)
    r = recording.ServerRecording(s)
    try:
        # Independent encoders isolate decoder history from encoder-side lookahead.
        muted = muted_encoder.encode(np.random.default_rng(1).integers(
            -30000, 30000, 320, dtype=np.int16).tobytes())
        live = live_encoder.encode(bytes(640))
        reference = Decoder(16000)
        try:
            expected = reference.decode(live)
        finally:
            reference.close()
        s.ignore_microphone(muted=True)
        s.feed_encoded_audio(transport, muted, r.audio)
        s.unmute()
        s.feed_encoded_audio(transport, live, r.audio)
        r.close()
        await r.wait_closed()
        for path in recording.ROOT.glob('*.wav'):
            with wave.open(str(path), 'rb') as wav:
                assert wav.getnframes() == 0
    finally:
        transport.close()
        muted_encoder.close()
        live_encoder.close()


async def test_mute_without_active_recorder_disarms_future_sessions(tmp_path, monkeypatch):
    import numpy as np
    from hermes_converse import loop

    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(loop.diagnostic, 'disarm', recording.disarm)
    loop.ConverseSession(np).ignore_microphone(muted=True)
    r = recording.ServerRecording(loop.ConverseSession(np))
    r.audio(bytes(640))
    await r.wait_closed()
    assert not recording.ROOT.exists()


async def test_mute_disarms_recording_across_unmute_and_reconnect(tmp_path, monkeypatch):
    import numpy as np
    import pytest
    from hermes_converse.audio_transport import AudioTransport
    from hermes_converse.loop import ConverseSession
    from hermes_converse.opus import Encoder

    configure(monkeypatch, tmp_path)
    try:
        transport, encoder = AudioTransport('opus', 16000, 16000), Encoder(16000)
    except OSError as exc:
        pytest.skip(str(exc))
    s = ConverseSession(np)
    r = recording.ServerRecording(s)
    try:
        s.feed_encoded_audio(transport, encoder.encode(bytes(640)), r.audio)
        s.ignore_microphone(muted=True)
        muted = np.random.default_rng(1).integers(-30000, 30000, 320, dtype=np.int16).tobytes()
        s.feed_encoded_audio(transport, encoder.encode(muted), r.audio)
        s.unmute()
        s.feed_encoded_audio(transport, encoder.encode(bytes(640)), r.audio)
        await asyncio.sleep(0)
        await asyncio.wait_for(r.wait_closed(), 2)
        replacement = recording.ServerRecording(ConverseSession(np))
        s.feed_encoded_audio(transport, encoder.encode(bytes(640)), replacement.audio)
        replacement.close()
        await replacement.wait_closed()
        paths = list(recording.ROOT.glob('*.wav'))
        assert len(paths) <= 1
        for path in paths:
            with wave.open(str(path), 'rb') as wav:
                assert wav.getnframes() in (0, 320)
    finally:
        r.close()
        transport.close()
        encoder.close()


async def test_disarm_discards_queue_and_waits_for_inflight_write(tmp_path, monkeypatch):
    import threading
    configure(monkeypatch, tmp_path)
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    writes = []
    monkeypatch.setattr(recording._Files, 'open', lambda self: True)
    monkeypatch.setattr(recording._Files, 'close', lambda self: None)
    def write(self, item):
        entered.set()
        release.wait(3)
        writes.append(item)
        return True
    monkeypatch.setattr(recording._Files, 'write', write)
    r = recording.ServerRecording(session())
    r.audio(b'first')
    assert await asyncio.to_thread(entered.wait, 3)
    r.audio(b'queued')
    t = threading.Thread(target=lambda: (recording.disarm(), done.set()))
    t.start()
    await asyncio.sleep(.05)
    assert not done.is_set()
    release.set()
    await asyncio.to_thread(t.join, 3)
    assert done.is_set()
    await asyncio.wait_for(r.wait_closed(), 3)
    assert writes == [b'first']


def test_muted_decode_preserves_normal_decoder_state():
    import numpy as np
    from hermes_converse.loop import ConverseSession
    class Audio:
        input_resampler = 'keep'
        input_carry = b'keep'
        decoder = SimpleNamespace(reset=lambda: (_ for _ in ()).throw(AssertionError('reset')))
        def decode(self, packet, target_rate):
            return bytes(640)
    s = ConverseSession(np)
    s.ignore_microphone(muted=True)
    audio = Audio()
    s.feed_encoded_audio(audio, b'packet', lambda data: None)
    assert audio.input_resampler == 'keep' and audio.input_carry == b'keep'
