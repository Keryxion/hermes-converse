"""Server-side proof that the converse pipeline hears speech — including QUIET speech — through the
REAL VAD, endpointer and WAV-capture path (only STT is mocked). This is the regression wall for
"I'm talking and the server just reports idle": it fails if the detector stops hearing a soft voice,
and it fails if it starts hearing silence or room noise."""

import queue as _queue
import time

import numpy as np
import pytest

from hermes_converse.loop import ConverseSession, QuietTick

from tests.support.converse_audio import (
    RMS_AT_ROOM_FLOOR, RMS_LOUD_SPEECH, RMS_NORMAL_SPEECH, RMS_QUIET_SPEECH, collect_session_events, heard,
    mocked_stt, room_noise, run_utterances, silence, speech_like)


class TestConverseHearsSpeechAcrossVolume:
    @pytest.mark.parametrize("rms", [RMS_QUIET_SPEECH, 700, 1000, RMS_NORMAL_SPEECH, RMS_LOUD_SPEECH])
    def test_speech_is_heard(self, rms):
        assert heard(speech_like(rms, seed=rms)), f"speech at {rms} RMS was not heard"

    def test_quiet_speech_across_several_takes(self):
        for seed in range(5):
            assert heard(speech_like(RMS_QUIET_SPEECH, seed=100 + seed)), f"soft take {seed} missed"


class TestConverseRejectsNonSpeech:
    def test_silence_is_not_heard(self):
        assert not heard(silence(), timeout=2.5)

    @pytest.mark.parametrize("rms", [120, 200, 250])
    def test_room_noise_is_not_heard(self, rms):
        assert not heard(room_noise(rms, seed=rms), timeout=2.5), f"room noise {rms} false-tripped"

    def test_signal_at_the_room_floor_is_not_heard(self):
        assert not heard(speech_like(RMS_AT_ROOM_FLOOR, seed=3), timeout=2.5)


class TestConverseMultiTurn:
    def test_three_utterances_yield_three_transcripts(self):
        segs = [speech_like(RMS_NORMAL_SPEECH, seed=s) for s in (11, 22, 33)]
        assert run_utterances(segs, transcript="ok", timeout=6.0) == ["ok", "ok", "ok"]

    def test_gap_between_utterances_is_not_its_own_turn(self):
        segs = [speech_like(RMS_QUIET_SPEECH, seed=44), speech_like(RMS_QUIET_SPEECH, seed=55)]
        got = run_utterances(segs, timeout=6.0)
        assert len(got) == 2, f"expected 2 turns, got {len(got)}: {got}"


class TestBoundaryIsDocumented:
    def test_400_rms_heard_250_noise_silent(self):
        assert heard(speech_like(400, seed=400)), "400 RMS speech should be heard"
        assert not heard(room_noise(250, seed=8), timeout=2.5), "250 RMS noise should be silent"


class TestNoisyRoomSelfCalibrates:
    def _run(self, segments, *, timeout=4.0):
        with mocked_stt("heard"):
            session = ConverseSession(np, input_rate=16000)
            session.start()
            try:
                for seg in segments:
                    session.stream.feed(seg.tobytes())
                got = []
                try:
                    while True:
                        item = session.transcripts.get(timeout=timeout)
                        if item is None:
                            break
                        if isinstance(item, str) and item:
                            got.append(item)
                            break
                except _queue.Empty:
                    pass
                return got
            finally:
                session.stop()

    def test_steady_tv_background_alone_does_not_chatter(self):
        got = self._run([room_noise(1500, seconds=3.0, seed=5)], timeout=3.0)
        assert got == [], f"TV background alone produced a spurious turn: {got}"

    def test_voice_over_tv_is_heard(self):
        segs = [room_noise(1500, seconds=1.5, seed=6), speech_like(3000, seconds=1.4, seed=7),
                room_noise(1500, seconds=1.6, seed=8)]
        assert self._run(segs, timeout=6.0) == ["heard"], "a voice over the TV should be heard"


class TestQuietTracksReceivedSilence:
    def test_streamed_silence_accrues_growing_quiet_then_speech_ends_it(self):
        events = collect_session_events(
            [silence(seconds=2.0), speech_like(RMS_NORMAL_SPEECH, seed=5), silence(seconds=1.5)],
            quiet_interval=0.5)
        ticks = [e for e in events if isinstance(e, QuietTick)]
        assert len(ticks) >= 2, f"expected quiet ticks from streamed silence, got {events!r}"
        qs = [t.quiet_seconds for t in ticks]
        assert qs == sorted(qs) and qs[0] > 0
        assert any(isinstance(e, str) and e for e in events)

    def test_quiet_is_suppressed_during_a_turn_and_restarts_from_zero_after(self):
        session = ConverseSession(np, quiet_interval=0.3)  # worker not started
        session.begin_turn()
        for _ in range(60):
            session._account_received_silence()
        assert session.transcripts.empty()
        session.end_turn()
        for _ in range(12):
            session._account_received_silence()
        ticks = []
        while not session.transcripts.empty():
            ticks.append(session.transcripts.get_nowait())
        first = next((t for t in ticks if isinstance(t, QuietTick)), None)
        assert first is not None and first.quiet_seconds <= 0.35

    def test_no_audio_produces_no_quiet_ticks(self):
        with mocked_stt("unused"):
            session = ConverseSession(np, quiet_interval=0.3)
            session.start()
            try:
                time.sleep(1.6)  # >5 intervals of wall-clock, but no audio is fed
                drained = []
                while not session.transcripts.empty():
                    drained.append(session.transcripts.get_nowait())
                assert not any(isinstance(x, QuietTick) for x in drained)
            finally:
                session.stop()
