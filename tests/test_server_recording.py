"""The restored diagnostic recorder stays off until explicitly enabled."""

import importlib
import wave

import numpy as np

from hermes_converse import vad
from hermes_converse import _server_recording as recording


def test_diagnostic_capture_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CONVERSE_DIAGNOSTIC_RECORDING", raising=False)
    importlib.reload(recording)
    monkeypatch.setattr(recording, "ROOT", tmp_path)
    wav = vad.write_wav(np, np.zeros(1600, dtype=np.int16))
    try:
        recording.save_capture(wav, "session")
        recording.ServerRecording(type("Session", (), {"_input_rate": 16000, "session_id": "session", "_detector": type("Detector", (), {"name": "energy"})()})())
        assert list(tmp_path.iterdir()) == []
    finally:
        vad.unlink_quietly(wav)


def test_diagnostic_capture_opt_in_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("CONVERSE_DIAGNOSTIC_RECORDING", "1")
    importlib.reload(recording)
    monkeypatch.setattr(recording, "ROOT", tmp_path)
    wav = vad.write_wav(np, np.zeros(1600, dtype=np.int16))
    try:
        recording.save_capture(wav, "session")
        assert len(list(tmp_path.glob("capture-*.wav"))) == 1
        recorder = recording.ServerRecording(type("Session", (), {"_input_rate": 16000, "session_id": "session", "_detector": type("Detector", (), {"name": "energy"})()})())
        recorder.audio(b"\x00\x00" * 1600)
        recorder.close()
        with wave.open(str(next(tmp_path.glob("input-*.wav"))), "rb") as output:
            assert output.getnframes() == 1600
        recording.captures = 20
        recording.save_capture(wav, "session")
        assert len(list(tmp_path.glob("capture-*.wav"))) == 1
    finally:
        vad.unlink_quietly(wav)
        monkeypatch.delenv("CONVERSE_DIAGNOSTIC_RECORDING", raising=False)
        importlib.reload(recording)