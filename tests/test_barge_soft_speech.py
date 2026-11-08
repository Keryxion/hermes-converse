"""Quiet speech is valid input at idle, but must not cancel an active reply.

The scripted model deliberately recognizes soft speech with high confidence:
speech probability alone is not evidence that the user intends to interrupt.
Real PCM goes through the actual session worker and interruption callbacks.
"""
import threading

import numpy as np
import pytest

from hermes_converse import speech, vad
from hermes_converse.loop import ConverseSession


def pcm_block(rms):
    wave = np.sin(np.arange(480) * 2 * np.pi * 200 / 16000)
    return np.round(wave * rms / np.sqrt(np.mean(wave ** 2))).astype(np.int16)


class ConfidentSpeech:
    def feed(self, block):
        return 0.99 if vad.rms(np, block) > 200 else 0.01


class FailedModel:
    def feed(self, block):
        raise RuntimeError('inference unavailable')


@pytest.mark.parametrize('backend', ['silero', 'energy', 'failed-silero'])
@pytest.mark.parametrize('phase', ['pending', 'thinking', 'speaking', 'tail'])
def test_soft_mumbling_does_not_cancel_but_deliberate_speech_does(monkeypatch, backend, phase):
    monkeypatch.setattr(speech, 'spawn_context_thread',
                        lambda target, **kw: threading.Thread(target=target, daemon=True))
    monkeypatch.setattr(speech, 'mark_speech_interrupted', lambda: None)
    session = ConverseSession(np, vad_model='energy',
                              transcribe=lambda path: pytest.fail('control audio must never reach STT'))
    detector = session._detector
    if backend == 'silero':
        detector._stream = ConfidentSpeech()
    for _ in range(30):
        detector.feed(pcm_block(60), False)
    if backend == 'failed-silero':
        detector._stream = FailedModel()
    stop, interrupted = threading.Event(), threading.Event()
    if phase == 'pending':
        session._pending_turn.set()
    else:
        session.begin_turn('reply', tts_stop=stop, on_interrupt=interrupted.set)
        if phase == 'speaking':
            session.set_playing(True, tts_stop=stop)
        elif phase == 'tail':
            session.end_turn()
            session.begin_playback_tail('reply', 30)
            session.on_tail_barge = lambda turn: interrupted.set()

    # Observe processing rather than using sleeps to guess when the worker ran.
    processed, target = 0, 0
    done = threading.Event()
    feed = detector.feed

    def observed(block, playing, **kwargs):
        nonlocal processed
        result = feed(block, playing, **kwargs)
        processed += 1
        if processed >= target:
            done.set()
        return result

    detector.feed = observed

    def deliver(levels):
        nonlocal target
        done.clear()
        target = processed + len(levels)
        session.stream.feed(np.concatenate([pcm_block(level) for level in levels]).tobytes())
        assert done.wait(3), 'worker did not process test PCM'
        with session._state_lock:
            pass  # the final block's session transition must have completed too

    def cancelled():
        return stop.is_set() or interrupted.is_set() or session._cancelled.is_set()

    session.start()
    try:
        deliver([60] * 30)  # stable room/playback baseline
        deliver([400, 900, 600, 1200, 800, 500, 1000] * 12 + [60] * 30)
        assert not cancelled(), f'{backend}/{phase}: soft mumbling cancelled the reply'
        assert session.transcripts.empty()
        # An above-threshold click/burst must not count as sustained interruption.
        deliver(([4500] * 3 + [60] * 17) * 3)
        assert not cancelled(), f'{backend}/{phase}: a short burst cancelled the reply'
        deliver([4500] * 3 + [900] * 30 + [60] * 30)
        assert not cancelled(), f'{backend}/{phase}: a brief noise over mumbling cancelled the reply'
        deliver([4500] * 30)
        assert cancelled(), f'{backend}/{phase}: deliberate sustained speech was ignored'
    finally:
        session.stop()
        session._worker.join(timeout=2)
        assert not session._worker.is_alive()
