"""A network pause must drop a partial utterance without creating silence or a later merged turn."""
import asyncio
import queue
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

from hermes_converse import loop as cl, vad
from hermes_converse.settings import DEFAULT_CAPTURE_NO_DATA_SECONDS, load_settings
from tests.test_arbitrate_integration import _connect, _patch, _speech_seams
from tests.test_route import Harness, _collect_turn
from tests.support.converse_audio import calibration, silence, speech_like


def test_timed_read_drops_partial_samples_and_half_sample():
    stream = cl.NetworkMicStream(np, stop=threading.Event(), poll_seconds=1)
    samples = np.array([123, 456], dtype=np.int16)
    stream.feed(samples.tobytes() + b'\xff')
    data, _ = stream.read(4, no_data_seconds=0.02)
    assert stream.no_data_return
    np.testing.assert_array_equal(data, [123, 456, 0, 0])
    fresh = np.array([11, 22, 33, 44], dtype=np.int16)
    stream.feed(fresh.tobytes())
    data, _ = stream.read(4, no_data_seconds=0.02)
    assert not stream.no_data_return
    np.testing.assert_array_equal(data, fresh)


def test_buffered_audio_is_not_dropped_just_because_its_feed_was_old():
    stream = cl.NetworkMicStream(np, stop=threading.Event())
    stream.feed(np.arange(12, dtype=np.int16).tobytes())
    stream._last_feed -= 10  # All complete buffered blocks still belong to this utterance.
    for offset in (0, 4, 8):
        data, _ = stream.read(4, no_data_seconds=0.02)
        assert not stream.no_data_return
        np.testing.assert_array_equal(data, np.arange(offset, offset + 4))
    stream.read(4, no_data_seconds=0.02)
    assert stream.no_data_return


def test_small_feeds_refresh_timeout_until_a_block_is_complete():
    stream = cl.NetworkMicStream(np, stop=threading.Event(), poll_seconds=0.005)
    with ThreadPoolExecutor() as executor:
        result = executor.submit(stream.read, 6, no_data_seconds=0.1)
        try:
            for value in range(6):
                threading.Event().wait(0.025)
                stream.feed(np.array([value], dtype=np.int16).tobytes())
            data, _ = result.result(timeout=1)
            assert not stream.no_data_return
            np.testing.assert_array_equal(data, np.arange(6))
        finally:
            stream.close()


@pytest.mark.parametrize('timeout', [None, 0, -1])
def test_idle_or_disabled_read_waits_for_real_audio(timeout):
    stream = cl.NetworkMicStream(np, stop=threading.Event(), poll_seconds=0.005)
    with ThreadPoolExecutor() as executor:
        result = executor.submit(stream.read, 4, no_data_seconds=timeout)
        try:
            threading.Event().wait(0.04)
            assert not result.done()
            stream.feed(np.arange(4, dtype=np.int16).tobytes())
            data, _ = result.result(timeout=1)
            assert not stream.no_data_return
            np.testing.assert_array_equal(data, np.arange(4))
        finally:
            stream.close()


def test_close_takes_priority_over_capture_timeout():
    stream = cl.NetworkMicStream(np, stop=threading.Event())
    stream.feed(np.array([77], dtype=np.int16).tobytes())
    stream._last_feed -= 10
    stream.close()
    data, _ = stream.read(4, no_data_seconds=0.01)
    assert not stream.no_data_return
    np.testing.assert_array_equal(data, [77, 0, 0, 0])


class CompleteTurn:
    threshold = 0.5

    def turn_complete_probability(self, audio):
        return 1.0


@pytest.mark.parametrize('adaptive', [False, True])
def test_starved_capture_skips_stt_removes_wav_and_clears_preroll(monkeypatch, adaptive):
    paths, heard = [], []
    write_wav = vad.write_wav

    def record_wav(*args, **kwargs):
        path = write_wav(*args, **kwargs)
        paths.append(path)
        return path

    def transcribe(path):
        with wave.open(path, 'rb') as wav:
            heard.append(np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16))
        return 'fresh utterance'

    monkeypatch.setattr(vad, 'write_wav', record_wav)
    session = cl.ConverseSession(np, vad_model='energy', capture_no_data_seconds=0.02,
                                 transcribe=transcribe)
    session.stream._poll_seconds = 0.005
    if adaptive:
        session._turn_detector = CompleteTurn()
    session._pre_roll.append(np.full(session._block, 12345, np.int16))
    assert session._capture_and_transcribe()[0] == ''
    assert not heard
    assert not session._pre_roll
    session._pre_roll.append(np.full(session._block, 23456, np.int16))
    session.stream.feed(silence(seconds=2).tobytes())
    assert session._capture_and_transcribe()[0] == 'fresh utterance'
    assert len(heard) == 1 and 12345 not in heard[0] and 23456 in heard[0]
    assert paths and all(not Path(path).exists() for path in paths)


@pytest.mark.parametrize('adaptive', [False, True])
def test_worker_releases_starved_capture_and_hears_next_utterance(adaptive):
    heard = []
    session = cl.ConverseSession(np, vad_model='energy', capture_no_data_seconds=0.05,
                                 quiet_interval=0.1, transcribe=lambda path: heard.append(path) or 'fresh')
    session.stream._poll_seconds = 0.005
    if adaptive:
        session._turn_detector = CompleteTurn()
    session.enable_turn_control()
    tripped = threading.Event()
    session.on_trip = lambda phase: tripped.set()
    session.start()
    try:
        session.stream.feed(calibration().tobytes())
        session.stream.feed(speech_like(4000, seconds=0.5, seed=7).tobytes())
        assert tripped.wait(1)
        while not isinstance(session.transcripts.get(timeout=1), cl.InputReady):
            pass
        assert not session.capture_pending() and not heard
        # The timed-out partial block must not become a QuietTick or wake another capture.
        with pytest.raises(queue.Empty):
            session.transcripts.get(timeout=0.1)
        session.stream.feed(speech_like(6000, seconds=0.5, seed=22).tobytes())
        session.stream.feed(silence(seconds=2).tobytes())
        while True:
            event = session.transcripts.get(timeout=2)
            if isinstance(event, str):
                assert event == 'fresh'
                break
        assert len(heard) == 1
    finally:
        session.stop()


@pytest.mark.parametrize('raw, expected', [
    (None, 5.0), ('bad', 5.0), (float('nan'), 5.0), (float('inf'), 5.0),
    (0, 0), (-1, 0), ('0.25', 0.25),
])
def test_capture_timeout_setting(raw, expected):
    settings = load_settings({'capture_no_data_seconds': raw}.get)
    assert settings.capture_no_data_seconds == expected
    assert DEFAULT_CAPTURE_NO_DATA_SECONDS == 5.0


@pytest.mark.asyncio
async def test_websocket_starvation_drops_old_capture_and_preserves_new_turn(monkeypatch):
    fake = _patch(monkeypatch)
    tripped = threading.Event()
    released = threading.Event()
    original_release = cl.ConverseSession.release_capture
    original_init = cl.ConverseSession.__init__

    def init(session, *args, **kwargs):
        original_init(session, *args, **kwargs)
        session.on_trip = lambda phase: tripped.set()
        assert session._capture_no_data_seconds == 0.05  # Settings reached the real worker.

    def release(session):
        original_release(session)
        released.set()

    monkeypatch.setattr(cl.ConverseSession, '__init__', init)
    monkeypatch.setattr(cl.ConverseSession, 'release_capture', release)
    async with Harness(fake, settings={'capture_no_data_seconds': 0.05, 'vad_model': 'energy'}) as client:
        ws = await _connect(client)
        await ws.send_bytes(calibration().tobytes())
        await ws.send_bytes(speech_like(4000, seconds=0.5, seed=7).tobytes())
        assert await asyncio.to_thread(tripped.wait, 2)
        assert await asyncio.to_thread(released.wait, 2)
        assert not fake.runs  # No stray agent run from the abandoned fragment.
        await ws.send_bytes(speech_like(6000, seconds=0.5, seed=22).tobytes())
        await ws.send_bytes(silence(seconds=2).tobytes())
        frames, pcm = await asyncio.wait_for(_collect_turn(ws), timeout=3)
        assert len(fake.runs) == 1
        assert any(frame['type'] == 'thinking' for frame in frames)
        assert frames[-1]['type'] == 'turn_done' and pcm
        await ws.close()
