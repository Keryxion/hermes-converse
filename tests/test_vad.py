"""The barge/onset detector as a pure RMS state machine, and the endpointer over the mic shim."""

import threading
import time
import wave
from collections import deque

import numpy as np
import pytest

from hermes_converse import vad
from hermes_converse.loop import NetworkMicStream


def _make_detector():
    return vad.BargeDetector(np, mult=vad.DEFAULT_BARGE_MULTIPLIER, calib_blocks=max(1, 450 // 30),
                             trip_blocks=max(1, 300 // 30), grace_blocks=max(0, 500 // 30))


def test_barge_detector_trips_on_speech_after_quiet_floor():
    det = _make_detector()
    for _ in range(20):
        assert det.feed(80.0, playing=False) is None
    tripped = None
    for _ in range(20):
        phase = det.feed(6000.0, playing=False)
        if phase is not None:
            tripped = phase
            break
    assert tripped == "generation"


def test_barge_detector_ignores_bleed_during_grace_window():
    det = _make_detector()
    for _ in range(20):
        det.feed(80.0, playing=False)
    tripped = False
    for _ in range(det.grace_blocks):
        if det.feed(1200.0, playing=True) is not None:
            tripped = True
    assert not tripped


class TestBargeDetectorOnset:
    """Onset (first-word) detection + ratchet + barge-in with the CLI defaults: these lock the fix
    for "the VAD thinks I'm not talking" when a browser mic's AGC pumps the floor toward speech."""

    PARAMS = dict(mult=3.0, calib_blocks=15, trip_blocks=10, grace_blocks=16)

    def _run(self, floor_rms, speech_rms, *, playing=False, calib=15, speech_blocks=20,
             pre_playback_blocks=0, pre_playback_rms=0.0):
        d = vad.BargeDetector(np, **self.PARAMS)
        for _ in range(calib):
            d.feed(floor_rms, False)
        for _ in range(pre_playback_blocks):
            d.feed(pre_playback_rms, True)
        tripped = None
        for i in range(speech_blocks):
            if d.feed(speech_rms, playing):
                tripped = i
                break
        return tripped, d.quiet_floor

    def test_quiet_room_normal_speech_trips(self):
        assert self._run(150, 2500)[0] is not None

    @pytest.mark.parametrize("floor,speech", [(800, 2000), (1500, 2500), (2000, 3000)])
    def test_elevated_floor_normal_speech_still_trips(self, floor, speech):
        assert self._run(floor, speech)[0] is not None, f"floor={floor} speech={speech} should trip"

    def test_speech_at_or_below_floor_does_not_trip(self):
        assert self._run(2000, 1500)[0] is None

    def test_pure_silence_does_not_trip(self):
        assert self._run(150, 0)[0] is None

    def test_quiet_mic_soft_speech_trips(self):
        assert self._run(50, 350)[0] is not None

    def test_quiet_room_low_noise_does_not_false_trip(self):
        assert self._run(50, 250)[0] is None

    def test_speech_does_not_ratchet_the_floor(self):
        _, quiet_floor = self._run(1500, 3000, speech_blocks=20)
        assert quiet_floor <= 1500 + 1, f"floor ratcheted up to {quiet_floor}"

    def test_playback_bleed_alone_never_trips(self):
        d = vad.BargeDetector(np, **self.PARAMS)
        for _ in range(15):
            d.feed(150, False)
        assert not any(d.feed(1200, True) for _ in range(30))

    def test_playback_real_speech_trips_after_grace(self):
        tripped, _ = self._run(150, 4000, playing=True, pre_playback_blocks=25, pre_playback_rms=1200)
        assert tripped is not None


def test_converse_detector_uses_the_noisy_room_profile():
    det = vad.converse_detector(np)
    assert det.trip_needed == round(10 * vad.CONVERSE_TRIP_FRACTION)
    assert det._onset_ceiling is None and det._floor_percentile == 50.0


def test_network_mic_stream_reads_exact_blocks_from_odd_chunks():
    stream = NetworkMicStream(np, stop=threading.Event())
    samples = np.arange(1000, dtype=np.int16)
    raw = samples.tobytes()
    for start in range(0, len(raw), 7):  # 7-byte chunks split samples across feeds
        stream.feed(raw[start:start + 7])
    got = []
    for _ in range(2):
        data, overflow = stream.read(480)
        assert overflow is False and data.dtype == np.int16 and data.shape == (480,)
        got.append(data)
    np.testing.assert_array_equal(np.concatenate(got), samples[:960])


def test_network_mic_stream_zero_pads_and_returns_on_stop():
    stop = threading.Event()
    stream = NetworkMicStream(np, stop=stop, poll_seconds=0.01)
    stream.feed(np.arange(100, dtype=np.int16).tobytes())
    stop.set()
    data, overflow = stream.read(480)
    assert overflow is False and data.shape == (480,)
    np.testing.assert_array_equal(data[:100], np.arange(100, dtype=np.int16))
    assert np.all(data[100:] == 0)


def test_capture_until_quiet_produces_wav_from_shim():
    stream = NetworkMicStream(np, stop=threading.Event(), poll_seconds=0.01)
    block = int(vad.SAMPLE_RATE * 0.03)
    speech = np.ones(block, dtype=np.int16) * 8000
    silence = np.zeros(block, dtype=np.int16)
    endpoint_blocks = max(1, 1250 // 30)
    stream.feed(speech.tobytes())
    stream.feed(speech.tobytes())
    for _ in range(endpoint_blocks + 2):
        stream.feed(silence.tobytes())
    wav_path, volume = vad.capture_until_quiet(stream, np, block, deque(maxlen=4), endpoint_blocks=endpoint_blocks,
                                               max_blocks=max(1, 30_000 // 30), sample_rate=22050)
    assert wav_path
    assert volume > 0  # the fed audio was loud, not silence
    with wave.open(wav_path, "rb") as wf:
        assert wf.getframerate() == 22050 and wf.getnchannels() == 1 and wf.getnframes() > 0
    vad.unlink_quietly(wav_path)


def test_network_mic_stream_flags_resume_after_a_gap_even_from_one_byte():
    stream = NetworkMicStream(np, stop=threading.Event(), poll_seconds=0.01)
    stream.resume_gap_seconds = 0.05
    stream.feed(np.zeros(480, dtype=np.int16).tobytes())
    stream.read(480)
    assert stream.resumed is False
    time.sleep(0.1)
    stream.feed(b"\x00")  # a lone odd byte queues nothing
    stream.feed(b"\x00" + np.zeros(479, dtype=np.int16).tobytes() + b"\x00")
    stream.read(480)
    assert stream.resumed is True
