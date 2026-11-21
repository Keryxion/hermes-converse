"""The reply's own audio comes back through the device's microphone (issue #10).

The server knows the audio and the text it sent. The level tests play a reply back into the
detector as a device would return it; the session tests run real PCM through the session worker
with speech to text scripted to say what the capture held.
"""
import asyncio
import threading
import time

import numpy as np
import pytest

from hermes_converse import echo, speech_detector as sd
from hermes_converse.loop import ConverseSession, InputReady

REPLY = ("Spotify is paused until you ask for it again. "
         "The kitchen speaker is still playing the news, and I can stop that too.")


def pcm_block(rms):
    wave = np.sin(np.arange(480) * 2 * np.pi * 200 / 16000)
    return np.round(wave * rms / np.sqrt(np.mean(wave ** 2))).astype(np.int16)


# ── the reply's words ──

@pytest.mark.parametrize('heard', [
    "Spotify is acquired until",                         # issue #10: one word misheard in four
    "spotify is paused until you ask for it again",      # read back exactly
    "The kitchen speaker is still playing the noose.",   # misheard at the end
    "is still playing the news and I can stop",          # a stretch from the middle, across a clause
    "Kitchen speaker still playing the news",            # speech to text dropped a word
])
def test_a_stretch_of_the_reply_read_back_is_its_echo(heard):
    assert echo.says_reply(heard, REPLY)


@pytest.mark.parametrize('heard', [
    "stop",                                              # too short to judge by its words
    "stop that",
    "Yes, stop that too.",                               # an answer reuses the question's words
    "No, play it in the kitchen.",
    "Actually pause the kitchen speaker instead of Spotify",
    "What time is it?",
    "again it for ask you until paused is Spotify",      # the reply's words, not its order
    "",
])
def test_a_request_is_not_the_replys_echo(heard):
    assert not echo.says_reply(heard, REPLY)


def test_nothing_is_an_echo_of_no_reply():
    assert not echo.says_reply("Spotify is paused until", "")


# ── the reply's level ──

def test_reply_level_is_the_loudest_sent_audio_a_microphone_could_be_hearing():
    level = echo.ReplyLevel(np)
    assert level.at(100.0) is None
    level.sent(pcm_block(2000).tobytes(), 16000, 100.0)   # 30 ms, due at t=100
    level.sent(pcm_block(8000).tobytes(), 16000, 101.0)   # due a second later
    assert level.at(99.5) is None                         # not playing yet
    assert level.at(100.0) == pytest.approx(2000, rel=0.01)
    assert level.at(100.5) == pytest.approx(2000, rel=0.01)   # still on its way back
    assert level.at(101.0) == pytest.approx(8000, rel=0.01)
    assert level.at(101.9) == pytest.approx(8000, rel=0.01)
    assert level.at(103.0) is None                        # long played out
    level.sent(b'\x10', 16000, 200.0)                     # a chunk that ends mid-sample
    level.sent(pcm_block(500).tobytes() + b'\x10', 16000, 200.0)
    assert level.at(200.0) == pytest.approx(500, rel=0.01)


class Speech:
    """A speech model that calls everything speech: an echo is the assistant's voice."""
    def feed(self, block):
        return 0.99


def play(detector, reply_levels, *, with_reference, voice=0, coupling=0.25):
    """Play a reply at *reply_levels* (one per 30 ms block); the microphone hears *coupling* of
    it as echo, plus *voice* over it. Returns the detector's events."""
    return [detector.feed(pcm_block(level * coupling + voice), True,
                          reference=level if with_reference else None)
            for level in reply_levels]


def detector():
    d = sd.SpeechDetector(np, model='energy')
    d._stream = Speech()
    for _ in range(30):
        d.feed(pcm_block(60), False)  # a quiet room before the reply
    return d


QUIET_THEN_LOUD = [4000] * 60 + [14000] * 60  # a reply whose second sentence is louder: +11 dB


def test_a_reply_that_gets_louder_is_heard_as_speech_without_its_level():
    # What issue #10 reported: the echo so far says how loud the reply was, not how loud it is.
    assert 'playback' in play(detector(), QUIET_THEN_LOUD, with_reference=False)


def test_a_reply_that_gets_louder_does_not_interrupt_itself():
    assert play(detector(), QUIET_THEN_LOUD, with_reference=True) == [None] * len(QUIET_THEN_LOUD)


@pytest.mark.parametrize('coupling', [0.05, 0.25, 0.6])
def test_the_echo_gate_follows_the_device_not_a_fixed_level(coupling):
    # A device that cancels well, one that does not, and a loud one: none needs a setting.
    events = play(detector(), QUIET_THEN_LOUD, with_reference=True, coupling=coupling)
    assert events == [None] * len(QUIET_THEN_LOUD)


def test_a_voice_raised_over_the_reply_still_interrupts_it():
    d = detector()
    assert play(d, [14000] * 60, with_reference=True) == [None] * 60
    # The echo is 3500; a voice that brings the microphone to twice that is a person.
    assert 'playback' in play(d, [14000] * 15, with_reference=True, voice=3500)


def test_a_voice_in_a_pause_of_the_reply_interrupts_it():
    d = detector()
    assert play(d, [14000] * 60, with_reference=True) == [None] * 60
    events = [d.feed(pcm_block(6000), True, reference=None) for _ in range(15)]
    assert 'playback' in events


# ── the session: a capture that began over a reply ──

async def capture_over_reply(heard, *, phase, reply=REPLY):
    """Start a reply, feed speech-level audio while it is in *phase*, and return what the
    session made of it: ``(items put for the driver, the reply was interrupted)``."""
    session = ConverseSession(np, vad_model='energy', transcribe=lambda path: heard)
    session.enable_turn_control()
    for _ in range(30):
        session._detector.feed(pcm_block(60), False)
    stop, interrupted = threading.Event(), threading.Event()
    if phase != 'idle':
        session.begin_turn('reply', tts_stop=stop, on_interrupt=lambda _stop_run: interrupted.set())
        session.reply_text(reply)
    if phase == 'speaking':
        session.set_playing(True, tts_stop=stop)
    elif phase == 'drain':
        session.end_turn()
        session.begin_playback_tail('reply', 30)
        session.on_tail_barge = lambda turn: interrupted.set()
    session.start()
    try:
        session.stream.feed(np.concatenate([pcm_block(60)] * 30 + [pcm_block(4500)] * 30
                                           + [pcm_block(60)] * 40).tobytes())
        item = await session.transcripts.get_async(5)
        return item, stop.is_set() or interrupted.is_set()
    finally:
        session.stop()
        await asyncio.wait_for(session._worker, 2)


@pytest.mark.parametrize('phase', ['speaking', 'drain'])
async def test_the_replys_own_audio_does_not_come_back_as_a_request(phase):
    # Issue #10: the microphone picked up the reply, and its words were treated as input.
    item, _ = await capture_over_reply("Spotify is acquired until", phase=phase)
    assert isinstance(item, InputReady)   # the capture is over, and there is no request


@pytest.mark.parametrize('phase', ['speaking', 'drain'])
async def test_a_request_spoken_over_the_reply_is_still_a_request(phase):
    item, interrupted = await capture_over_reply("No, play it in the kitchen.", phase=phase)
    assert str(item) == "No, play it in the kitchen." and interrupted


async def test_a_short_word_over_the_reply_is_a_request_even_when_the_reply_says_it():
    item, interrupted = await capture_over_reply("stop", phase='speaking')
    assert str(item) == "stop" and interrupted


async def test_repeating_the_reply_after_it_has_played_is_a_request():
    # Only a capture that began while the reply was playing can be its echo.
    item, _ = await capture_over_reply("Spotify is paused until you ask for it again", phase='idle')
    assert str(item) == "Spotify is paused until you ask for it again"


async def test_an_echo_is_judged_against_the_reply_it_began_over():
    session = ConverseSession(np, vad_model='energy', transcribe=lambda path: "unused")
    session.begin_turn('first', tts_stop=threading.Event())
    session.reply_text(REPLY)
    over = session._reply_words
    session.end_turn()
    session.begin_turn('second', tts_stop=threading.Event())
    session.reply_text("Something else entirely.")
    assert over == [REPLY] and session._reply_words == ["Something else entirely."]


async def test_the_session_gives_the_detector_the_level_of_the_reply_it_sent():
    session = ConverseSession(np, vad_model='energy', transcribe=lambda path: "")
    seen = []
    feed = session._detector.feed

    def observed(block, playing, **kwargs):
        seen.append((playing, kwargs.get('reference')))
        return feed(block, playing, **kwargs)

    session._detector.feed = observed
    stop = threading.Event()
    session.begin_turn('reply', tts_stop=stop)
    session.set_playing(True, tts_stop=stop)
    session.reply_audio(pcm_block(9000).tobytes() * 40, 16000, time.monotonic())
    session.start()
    try:
        session.stream.feed(np.concatenate([pcm_block(60)] * 5).tobytes())
        for _ in range(100):
            if len(seen) >= 5:
                break
            await asyncio.sleep(0.01)
    finally:
        session.stop()
        await asyncio.wait_for(session._worker, 2)
    assert seen and all(playing and reference == pytest.approx(9000, rel=0.01) for playing, reference in seen)
