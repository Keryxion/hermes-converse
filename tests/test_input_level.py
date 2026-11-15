"""The session's input gain (level.py, specs/audio.md): a quiet device's speech is heard, a normal one
is left alone, and the gain holds still while a turn is under way."""
import threading
import wave
from pathlib import Path

import numpy as np
import pytest

from hermes_converse import level, speech
from hermes_converse.level import InputLevel
from hermes_converse.loop import ConverseSession
from hermes_converse.silero_vad import load_silero_session as real_silero  # before conftest stubs it

JFK = Path(__file__).parent / "fixtures" / "jfk-4s.wav"


def noise(rms, n, seed=1):
    return np.random.default_rng(seed).normal(0, rms, n)


def block(rms, seed=1):
    return np.clip(np.round(noise(rms, 480, seed)), -32768, 32767).astype(np.int16)


def test_quiet_floor_is_brought_up_and_a_normal_one_left_alone():
    quiet = InputLevel(np)
    for i in range(40):
        quiet.apply(block(3, i), learn=True)
    assert 11 <= quiet.gain <= 16  # TARGET_FLOOR 40 over a floor near 3: about +22 dB
    normal = InputLevel(np)
    for i in range(40):
        normal.apply(block(120, i), learn=True)
    assert normal.gain == 1.0


def test_gain_is_capped_and_waits_for_enough_blocks():
    lv = InputLevel(np)
    faint = np.array([1, -1] * 240, dtype=np.int16)      # RMS 1: the quietest real floor
    for _ in range(level.MIN_BLOCKS - 1):
        lv.apply(faint, learn=True)
    assert lv.gain == 1.0
    lv.apply(faint, learn=True)
    assert lv.gain == pytest.approx(min(level.MAX_GAIN, level.TARGET_FLOOR))


def test_digital_silence_does_not_teach_the_gain():
    lv = InputLevel(np)
    for _ in range(100):
        lv.apply(np.zeros(480, dtype=np.int16), learn=True)
    assert lv.gain == 1.0


def test_gain_holds_while_not_learning_and_clips():
    lv = InputLevel(np)
    for i in range(40):
        lv.apply(block(3, i), learn=True)
    g = lv.gain
    loud = np.full(480, 20000, dtype=np.int16)
    for _ in range(50):
        out = lv.apply(loud, learn=False)  # a turn: speech and playback do not move it
    assert lv.gain == g
    assert out.dtype == np.int16 and out.max() == 32767


def _jfk():
    with wave.open(str(JFK)) as w:
        assert w.getframerate() == 16000
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float64)


@pytest.mark.parametrize("attenuation_db", [0, 30, 40, 50])
@pytest.mark.parametrize("gain", [True, False])
def test_quiet_speech_is_heard_on_an_echo_floor(monkeypatch, attenuation_db, gain):
    """specs/audio.md acceptance: the request is captured at its own level and 30, 40 and 50 dB lower,
    over an Echo Dot 2's floor (RMS 3). Without the gain the 50 dB one (an Echo's close speech, about
    -65 dBFS, 16 dB over its floor) is missed, as on the Echo."""
    session_loader = lambda: real_silero()  # noqa: E731
    monkeypatch.setattr("hermes_converse.speech_detector.load_silero_session", session_loader)
    monkeypatch.setattr("hermes_converse.silero_vad.load_silero_session", session_loader)
    if real_silero()[0] is None:
        pytest.skip("Silero model not available")
    if not gain:
        monkeypatch.setattr(InputLevel, "apply", lambda self, b, learn: b)
    monkeypatch.setattr(speech, "spawn_context_thread",
                        lambda target, **kw: threading.Thread(target=target, daemon=True))
    session = ConverseSession(np, vad_model="silero", transcribe=lambda path: "heard")
    assert session._detector.name == "silero"
    speech_pcm = _jfk() * 10 ** (-attenuation_db / 20)
    pcm = np.concatenate([noise(3, 32000, 1), speech_pcm + noise(3, len(speech_pcm), 2), noise(3, 48000, 3)])
    session.start()
    try:
        session.stream.feed(np.clip(np.round(pcm), -32768, 32767).astype(np.int16).tobytes())
        try:
            heard = session.transcripts.get(timeout=8)
        except Exception:
            heard = None
    finally:
        session.stop()
        session._worker.join(timeout=2)
    if gain or attenuation_db < 50:
        assert heard == "heard", f"speech {attenuation_db} dB down over the Echo's floor was not captured"
    else:
        assert heard is None, "expected the ungained Echo level to be missed (the bug this fixes)"


def test_a_devices_floor_carries_over_to_its_next_session(monkeypatch, tmp_path):
    """An Echo reconnects on every server restart: its next session starts from the floor it had."""
    store = tmp_path / "input-levels.json"
    monkeypatch.setattr(level, "_store_path", lambda: store)
    monkeypatch.setattr(level, "_floors", None)
    first = InputLevel(np)
    first.attach("Den Echo")
    for i in range(40):
        first.apply(block(3, i), learn=True)
    learned = first.gain
    assert learned > 5 and store.is_file()
    monkeypatch.setattr(level, "_floors", None)            # a server restart: read back from the file
    second = InputLevel(np)
    second.attach("Den Echo")
    assert second.gain == pytest.approx(learned, rel=0.2)
    other = InputLevel(np)
    other.attach("Kitchen")
    assert other.gain == 1.0


def test_a_sentence_nobody_detected_yet_does_not_lower_the_gain():
    """Blocks are learned until an onset; a far voice's 6 s sentence must not raise the floor with it
    (sakura, 2026-11-15: the gain fell from +18 to +1.6 dB during one and the speech went unheard)."""
    lv = InputLevel(np)
    for i in range(100):
        lv.apply(block(4, i), learn=True)
    before = lv.gain_db
    for i in range(200):                                   # 6 s of speech-loud blocks, still "idle"
        lv.apply(block(40, 1000 + i), learn=True)
    assert before - lv.gain_db < 6.5, (before, lv.gain_db)
    for i in range(100):                                   # the room again: back at once
        lv.apply(block(4, 2000 + i), learn=True)
    assert abs(lv.gain_db - before) < 1.0


def test_stt_copy_is_one_gain_high_passed_and_never_quieter():
    rate = 16000
    t = np.arange(rate * 2) / rate
    voice = 600 * np.sin(2 * np.pi * 300 * t) * np.where(t < 1, 0.5, 1.0)  # quiet, its second half twice the first
    hum = 400 * np.sin(2 * np.pi * 50 * t)
    out = level.for_stt(np, (voice + hum).astype(np.int16), rate).astype(np.float64)
    assert np.percentile(np.abs(out), 99.9) > 0.6 * 32767                    # brought up to about -3 dBFS
    first, second = np.sqrt(np.mean(out[2000:14000] ** 2)), np.sqrt(np.mean(out[18000:30000] ** 2))
    assert abs(second / first - 2) < 0.1                                     # one gain: the ratio stays
    spectrum = np.abs(np.fft.rfft(out)); f = np.fft.rfftfreq(out.size, 1 / rate)
    assert spectrum[(f > 45) & (f < 55)].max() < 1e-3 * spectrum.max()       # the 50 Hz hum is gone
    loud = (20000 * np.sin(2 * np.pi * 300 * t)).astype(np.int16)
    assert np.abs(level.for_stt(np, loud, rate)).max() >= 19000               # never quieter than received
