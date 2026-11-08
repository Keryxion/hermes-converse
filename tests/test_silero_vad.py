"""Model framing/cache safety plus deterministic and real-model speech regression tests."""
import hashlib
import io
import os
from pathlib import Path
import sys
import wave

import numpy as np
import pytest

from hermes_converse import silero_vad as sv, speech_detector as sd, vad
from hermes_converse.loop import ConverseSession
from hermes_converse.settings import Settings, load_settings

# Retain the loader entry point before the suite's offline autouse fixture patches it.
_load_session = sv.load_silero_session


class RecordingSession:
    def __init__(self, probabilities=()):
        self.calls = []
        self.probabilities = iter(probabilities)

    def run(self, outputs, feeds):
        self.calls.append({key: value.copy() for key, value in feeds.items()})
        return np.array([[next(self.probabilities, 0)]], dtype=np.float32), feeds['state'] + 1


def test_framing_preserves_samples_context_and_connection_state():
    model = RecordingSession()
    stream = sv.SileroStream(model, np)
    audio = np.arange(16 * 480, dtype=np.int16)
    for block in np.split(audio, 16):
        stream.feed(block)
    assert len(model.calls) == 15
    for index, call in enumerate(model.calls):
        assert call['input'].shape == (1, 576)
        np.testing.assert_array_equal(call['input'][0, 64:], audio[index*512:(index+1)*512] / np.float32(32768))
        previous = model.calls[index-1]['input'][0, -64:] if index else np.zeros(64)
        np.testing.assert_array_equal(call['input'][0, :64], previous)
        assert np.all(call['state'] == index)
    other = sv.SileroStream(model, np)
    other.feed(np.zeros(512, dtype=np.int16))
    assert not model.calls[-1]['state'].any()
    assert not model.calls[-1]['input'].any()


@pytest.mark.parametrize('rate', [8000, 22050, 44100, 48000])
def test_resampling_is_continuous_across_network_blocks(rate):
    # Chunk boundaries must not change resampled content or introduce aliasing/state resets.
    samples = (np.sin(np.arange(rate) * 2 * np.pi * 220 / rate) * 5000).astype(np.int16)
    whole_model, chunk_model = RecordingSession(), RecordingSession()
    sv.SileroStream(whole_model, np, input_rate=rate).feed(samples)
    stream = sv.SileroStream(chunk_model, np, input_rate=rate)
    for offset in range(0, len(samples), max(1, rate * 30 // 1000)):
        stream.feed(samples[offset:offset+max(1, rate * 30 // 1000)])
    assert len(whole_model.calls) == len(chunk_model.calls) >= 30
    for whole, chunk in zip(whole_model.calls, chunk_model.calls):
        np.testing.assert_allclose(whole['input'], chunk['input'], atol=1e-6)


def test_probability_waits_for_full_model_window():
    stream = sv.SileroStream(RecordingSession([0.2, 0.9]), np)
    assert stream.feed(np.zeros(480, np.int16)) == 0
    assert stream.feed(np.zeros(480, np.int16)) == pytest.approx(0.2)
    assert stream.feed(np.zeros(480, np.int16)) == pytest.approx(0.9)


@pytest.mark.parametrize('corrupt', [False, True])
def test_download_verified_and_cached_atomically(tmp_path, monkeypatch, corrupt):
    path = tmp_path / 'model.onnx'
    content = b'verified model'
    if corrupt:
        path.write_bytes(b'bad cached model')
    calls = []
    monkeypatch.setattr(sv, '_model_path', lambda: path)
    monkeypatch.setattr(sv, '_MODEL_SHA256', hashlib.sha256(content).hexdigest())
    def download(url, timeout):
        calls.append((url, timeout))
        return io.BytesIO(content)
    monkeypatch.setattr(sv.urllib.request, 'urlopen', download)
    assert sv._ensure_model() == path
    assert path.read_bytes() == content
    assert sv._ensure_model() == path
    assert len(calls) == 1 and calls[0][1] == 15
    assert list(tmp_path.iterdir()) == [path]


def test_bad_download_is_not_installed(tmp_path, monkeypatch):
    path = tmp_path / 'model.onnx'
    monkeypatch.setattr(sv, '_model_path', lambda: path)
    monkeypatch.setattr(sv.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(b'wrong model'))
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        sv._ensure_model()
    assert not list(tmp_path.iterdir())


def test_missing_runtime_is_cached_without_download(monkeypatch):
    monkeypatch.setattr(sv, '_session', None)
    monkeypatch.setattr(sv, '_failure', None)
    monkeypatch.setitem(sys.modules, 'onnxruntime', None)
    monkeypatch.setattr(sv, '_ensure_model', lambda: pytest.fail('must not download without ONNX Runtime'))
    session, reason = _load_session()
    assert session is None and 'onnxruntime unavailable' in reason
    assert _load_session() == (None, reason)


class ScriptedStream:
    def __init__(self, probabilities):
        self.probabilities = iter(probabilities)
        self.calls = 0

    def feed(self, block):
        self.calls += 1
        return next(self.probabilities)


def scripted(monkeypatch, probabilities):
    detector = sd.SpeechDetector(np, model='energy')
    detector._stream = ScriptedStream(probabilities)
    return detector


def test_onset_hysteresis_and_short_burst_rejection(monkeypatch):
    block = np.full(480, 150, np.int16)  # Soft speech can be below the energy trigger.
    detector = scripted(monkeypatch, [0.42]*10 + [0.9]*3 + [0.1] + [0.9]*3 + [0.42]*6)
    events = [detector.feed(block, False) for _ in range(23)]
    assert events[:-1] == [None]*22
    assert events[-1] == 'generation'
    assert detector._stream.calls == 23
    assert detector.quiet is False  # Does not advance recurrent state again.


def test_capture_quiet_uses_speech_off_threshold(monkeypatch):
    detector = scripted(monkeypatch, [0.34, 0.35])
    assert detector.is_quiet(np.ones(480, np.int16))
    assert not detector.is_quiet(np.ones(480, np.int16))


def test_echo_rejected_and_speech_over_echo_detected(monkeypatch):
    # For the loop, playback and its drain tail both pass playing=True.
    detector = scripted(monkeypatch, [0.99]*60)
    echo = [1200, 1500, 900, 1400]*9
    for level in echo:
        assert detector.feed(np.full(480, level, np.int16), True) is None
    events = [detector.feed(np.full(480, 4000, np.int16), True) for _ in range(12)]
    assert 'playback' in events


def test_inference_failure_switches_to_warm_energy_detector(monkeypatch):
    detector = sd.SpeechDetector(np, model='energy')
    for _ in range(20):
        detector.feed(np.full(480, 50, np.int16), False)
    class Broken:
        def feed(self, block):
            raise RuntimeError('runtime failed')
    detector._stream = Broken()
    events = [detector.feed(np.full(480, 3000, np.int16), False) for _ in range(12)]
    assert detector.name == 'energy' and 'inference failed' in detector.fallback_reason
    assert 'generation' in events
    detector._stream = Broken()
    assert detector.is_quiet(np.zeros(480, np.int16))
    assert detector.name == 'energy'


def test_explicit_energy_never_loads_model(monkeypatch):
    monkeypatch.setattr(sd, 'load_silero_session', lambda: pytest.fail('explicit energy must stay offline'))
    assert sd.SpeechDetector(np, model='energy').name == 'energy'


def test_default_settings_and_fallback(monkeypatch):
    assert Settings().vad_model == load_settings({}.get).vad_model == 'silero'
    assert load_settings({'vad_model': 'ENERGY'}.get).vad_model == 'energy'
    assert load_settings({'vad_model': 'unknown'}.get).vad_model == 'silero'
    detector = sd.SpeechDetector(np)
    assert detector.name == 'energy' and detector.fallback_reason


@pytest.fixture
def real_model(monkeypatch):
    path = os.environ.get('HERMES_TEST_SILERO_MODEL')
    if not path:
        pytest.skip('set HERMES_TEST_SILERO_MODEL to a cached upstream model; tests never download weights')
    ort = pytest.importorskip('onnxruntime')
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == sv._MODEL_SHA256
    options = ort.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    session = ort.InferenceSession(path, sess_options=options, providers=['CPUExecutionProvider'])
    monkeypatch.setattr(sd, 'load_silero_session', lambda: (session, None))
    return session


def speech_fixture():
    with wave.open(str(Path(__file__).parent / 'fixtures/jfk-4s.wav'), 'rb') as wav:
        assert wav.getframerate() == 16000
        return np.frombuffer(wav.readframes(wav.getnframes()), np.int16).copy()


@pytest.mark.parametrize('gain', [0.1, 1.0])
@pytest.mark.parametrize('smart_turn', [False, True])
def test_real_speech_captures_and_endpoints_with_silero(real_model, gain, smart_turn):
    from hermes_converse import speech
    from unittest.mock import patch
    audio = (speech_fixture().astype(np.float32)*gain).astype(np.int16)
    transcribed = []
    def transcribe(path):
        with wave.open(path, 'rb') as wav:
            transcribed.append(wav.getnframes())
        return 'real speech'
    session = ConverseSession(np, vad_model='silero', transcribe=transcribe)
    assert session._detector.name == 'silero'
    class SmartTurn:
        calls = 0
        def turn_complete_probability(self, audio):
            self.calls += 1
            return 0.1 if self.calls == 1 else 0.9
    if smart_turn:
        session._turn_detector = SmartTurn()
    # Worker creation is the only Hermes-dependent seam in this test.
    import threading
    with patch.object(speech, 'spawn_context_thread', lambda target, **kw: threading.Thread(target=target, daemon=True)):
        session.start()
    try:
        session.stream.feed(np.concatenate([np.zeros(16000, np.int16), audio,
                                            np.zeros(32000, np.int16)]).tobytes())
        assert session.transcripts.get(timeout=3) == 'real speech'
        assert 16000 < transcribed[0] < 8*16000
        if smart_turn:
            assert session._turn_detector.calls == 2
    finally:
        session.stop()
        session._worker.join(timeout=2)


def test_real_model_rejects_noise(real_model):
    detector = sd.SpeechDetector(np)
    noise = np.random.default_rng(1).normal(0, 1500, 3*16000).astype(np.int16)
    assert all(detector.feed(block, False) is None for block in np.split(noise, 100))


def test_playback_baseline_is_available_after_about_half_a_second(monkeypatch):
    detector = scripted(monkeypatch, [0.99]*32)
    for _ in range(16):
        assert detector.feed(np.full(480, 1200, np.int16), True) is None
    # The older port accidentally added 300 ms lag to a full 500 ms warm-up.
    assert 'playback' in [detector.feed(np.full(480, 4000, np.int16), True) for _ in range(12)]


def test_active_turn_speech_interrupts_without_stt_and_is_evaluated_once(monkeypatch):
    import threading
    from hermes_converse import speech
    detector = scripted(monkeypatch, [0.9]*30)
    interrupted = threading.Event()
    session = ConverseSession(np, vad_model='energy',
                              transcribe=lambda path: pytest.fail('control audio must never reach STT'))
    session._detector = detector
    session.begin_turn('test-turn', on_interrupt=interrupted.set)
    monkeypatch.setattr(speech, 'spawn_context_thread',
                        lambda target, **kw: threading.Thread(target=target, daemon=True))
    session.start()
    try:
        session.stream.feed(np.full(30*480, 5000, np.int16).tobytes())
        assert interrupted.wait(timeout=2)
    finally:
        session.stop()
        session._worker.join(timeout=2)
    assert detector._stream.calls <= 30  # discard-until-quiet reads the cached decision
    assert not any(isinstance(item, str) for item in list(session.transcripts.queue))


def test_download_failure_cleans_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / 'model.onnx'
    monkeypatch.setattr(sv, '_model_path', lambda: path)
    def timeout(*a, **kw):
        raise TimeoutError('unreachable')
    monkeypatch.setattr(sv.urllib.request, 'urlopen', timeout)
    with pytest.raises(TimeoutError):
        sv._ensure_model()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('playing', [False, True], ids=['thinking', 'speaker-or-tail'])
@pytest.mark.parametrize('recorded', [False, True], ids=['public-speech', 'recorded-room'])
def test_real_quiet_speech_is_valid_input_but_not_an_interruption(real_model, playing, recorded):
    if recorded:
        path = os.environ.get('HERMES_TEST_BARGE_AUDIO')
        if not path:
            pytest.skip('set HERMES_TEST_BARGE_AUDIO to a reviewed local 16 kHz mono PCM16 recording')
        with wave.open(path, 'rb') as wav:
            assert wav.getnchannels() == 1 and wav.getsampwidth() == 2 and wav.getframerate() == 16000
            original = np.frombuffer(wav.readframes(wav.getnframes()), np.int16).astype(np.float64)
    else:
        original = speech_fixture().astype(np.float64)
    block_peak = max(vad.rms(np, original[i:i + 480]) for i in range(0, original.size, 480))
    assert block_peak > 0
    quiet_speech = np.round(original * (900 / block_peak)).astype(np.int16)
    silence = np.zeros(480, np.int16)

    def blocks(audio):
        for start in range(0, audio.size, 480):
            yield np.pad(audio[start:start + 480], (0, max(0, start + 480 - audio.size)))

    # Do not solve false interruptions by making quiet voices inaudible at idle.
    idle = sd.SpeechDetector(np)
    for _ in range(40):
        idle.feed(silence, False)
    assert any(idle.feed(block, False) == 'generation' for block in blocks(quiet_speech))

    active = sd.SpeechDetector(np)
    for _ in range(40):
        assert active.feed(silence, playing, interrupting=True) is None
    assert all(active.feed(block, playing, interrupting=True) is None for block in blocks(quiet_speech))
    for _ in range(40):
        assert active.feed(silence, playing, interrupting=True) is None
    # The same human voice at ordinary volume must still interrupt.
    assert any(active.feed(block, playing, interrupting=True) is not None
               for block in blocks(original.astype(np.int16)))
