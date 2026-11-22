"""The agent's hold on the microphone for one turn: `mute` from the call, and `no_barge_in`.

Real PCM goes through the session worker. Each test asserts what happened to the reply (was it
interrupted) and to the speech (did it become a request).
"""
import asyncio
import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from hermes_converse.conversation import ConversationHub
from hermes_converse.loop import ConverseSession, ConversationOutput
from hermes_converse.server_turn import ServerTurn
from hermes_converse.voice_tools import SCHEMAS, make_handlers


def pcm_block(rms):
    wave = np.sin(np.arange(480) * 2 * np.pi * 200 / 16000)
    return np.round(wave * rms / np.sqrt(np.mean(wave ** 2))).astype(np.int16)


SPEECH = [4500] * 30 + [60] * 40   # sustained, loud: interrupts any reply that can be interrupted
QUIET = [60] * 30


class Reply:
    """A session with a reply under way in *phase*, and what became of the reply."""

    def __init__(self, phase):
        self.session = ConverseSession(np, vad_model='energy', transcribe=lambda path: 'what was said')
        for _ in range(30):
            self.session._detector.feed(pcm_block(60), False)
        self.stop = threading.Event()
        self._cut = threading.Event()
        self.session.begin_turn('reply', tts_stop=self.stop, on_interrupt=lambda _stop_run: self._cut.set())
        if phase == 'speaking':
            self.session.set_playing(True, tts_stop=self.stop)
        elif phase == 'drain':
            self.session.end_turn()
            self.session.begin_playback_tail('reply', 30)
            self.session.on_tail_barge = lambda turn: self._cut.set()
        self._seen = 0
        feed = self.session._detector.feed

        def counted(block, playing, **kwargs):
            self._seen += 1
            return feed(block, playing, **kwargs)

        self.session._detector.feed = counted

    @property
    def interrupted(self):
        return self.stop.is_set() or self._cut.is_set()

    async def hear(self, levels):
        """Send audio and wait until the session has been through all of it."""
        target = self._seen + len(levels)
        self.session.stream.feed(np.concatenate([pcm_block(level) for level in levels]).tobytes())
        for _ in range(500):
            if self._seen >= target or not self.session.transcripts.empty():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # the last block's transition

    async def request(self):
        return str(await self.session.transcripts.get_async(3))

    def nothing_heard(self):
        return self.session.transcripts.empty() and not self.session.capture_pending()


@pytest.fixture
async def reply(request):
    made = []

    def make(phase):
        made.append(Reply(phase))
        made[-1].session.start()
        return made[-1]

    yield make
    for item in made:
        item.session.stop()
        await asyncio.wait_for(item.session._worker, 2)


PHASES = ['thinking', 'speaking', 'drain']


@pytest.mark.parametrize('phase', PHASES)
async def test_speech_interrupts_a_reply_by_default(reply, phase):
    r = reply(phase)
    await r.hear(QUIET + SPEECH)
    assert await r.request() == 'what was said' and r.interrupted


@pytest.mark.parametrize('phase', PHASES)
@pytest.mark.parametrize('muted', [False, True])
async def test_microphone_audio_does_not_interrupt_a_held_turn(reply, phase, muted):
    r = reply(phase)
    r.session.ignore_microphone(muted=muted)
    await r.hear(QUIET + SPEECH)
    assert not r.interrupted and r.nothing_heard()


@pytest.mark.parametrize('muted', [False, True])
async def test_the_hold_ends_with_the_turns_playback(reply, muted):
    r = reply('speaking')
    r.session.ignore_microphone(muted=muted)
    await r.hear(QUIET + SPEECH)
    r.session.set_playing(False)
    r.session.finish_turn('reply', 30)                # sent; still playing on the device
    await r.hear(QUIET + SPEECH)
    assert not r.interrupted and r.nothing_heard()   # still playing on the device
    r.session.playback_ended('reply')
    await r.hear(QUIET + SPEECH)
    assert await r.request() == 'what was said'      # the next turn is an ordinary one


@pytest.mark.parametrize('phase', PHASES)
async def test_the_wake_word_interrupts_a_no_barge_in_turn_and_what_follows_is_heard(reply, phase):
    r = reply(phase)
    r.session.ignore_microphone(muted=False)
    await r.hear(QUIET + SPEECH)
    assert not r.interrupted
    r.session.heard()
    await r.hear([60] * 3)
    assert r.interrupted                              # the wake word alone stops the reply
    await r.hear(SPEECH)
    assert await r.request() == 'what was said'


async def test_the_wake_word_does_not_interrupt_an_ordinary_turn(reply):
    r = reply('speaking')
    r.session.heard()
    await r.hear(QUIET)
    assert not r.interrupted and r.nothing_heard()


async def test_a_muted_turn_does_not_hear_the_wake_word(reply):
    r = reply('speaking')
    r.session.ignore_microphone(muted=True)
    r.session.heard()
    await r.hear(QUIET + SPEECH)
    assert not r.interrupted and r.nothing_heard()


@pytest.mark.parametrize('phase', PHASES)
@pytest.mark.parametrize('muted', [False, True])
async def test_cancel_stops_a_held_turn_and_the_microphone_is_ordinary_again(reply, phase, muted):
    r = reply(phase)
    r.session.ignore_microphone(muted=muted)
    assert r.session.cancel('reply') and r.interrupted
    if phase == 'drain':
        r.session.playback_ended('reply')             # the device discarded what was queued
    else:
        r.session.end_turn()
    await r.hear(QUIET + SPEECH)
    assert await r.request() == 'what was said'


@pytest.mark.parametrize('muted', [False, True])
async def test_a_new_activation_ends_the_hold(reply, muted):
    r = reply('drain')
    r.session.ignore_microphone(muted=muted)
    r.session.note_wake()                             # Talk, or the wake word after unmuting
    await r.hear(QUIET + SPEECH + [60] * 120)         # a wake's capture waits out a longer pause
    assert await r.request() == 'what was said'


async def test_standby_after_mute_in_one_turn_leaves_the_microphone_heard(reply):
    r = reply('thinking')
    r.session.ignore_microphone(muted=True)
    r.session.unmute()
    await r.hear(QUIET + SPEECH)
    assert await r.request() == 'what was said' and r.interrupted


async def test_mute_is_not_weakened_by_no_barge_in(reply):
    r = reply('speaking')
    r.session.ignore_microphone(muted=True)
    r.session.ignore_microphone(muted=False)
    r.session.heard()
    await r.hear(QUIET + SPEECH)
    assert not r.interrupted and r.nothing_heard()


async def test_speech_accepted_before_the_call_is_still_a_request(reply):
    r = reply('thinking')
    r.session.stream.feed(np.concatenate([pcm_block(level) for level in QUIET + [4500] * 30]).tobytes())
    for _ in range(300):
        if r.session.capture_pending():
            break
        await asyncio.sleep(0.01)
    assert r.interrupted                              # it had already taken the floor
    r.session.ignore_microphone(muted=True)
    r.session.stream.feed(np.concatenate([pcm_block(60)] * 40).tobytes())
    assert await r.request() == 'what was said'


# ── the tools ──

@pytest.fixture
def tools(monkeypatch):
    env = {"HERMES_SESSION_PLATFORM": "converse", "HERMES_SESSION_CHAT_ID": "den"}
    monkeypatch.setattr("gateway.session_context.get_session_env", lambda key, default="": env.get(key, default))
    hub = ConversationHub()
    calls = []
    session = SimpleNamespace(stopped=False, ignore_microphone=lambda *, muted: calls.append(('ignore', muted)),
                              unmute=lambda: calls.append(('unmute',)), transcripts=SimpleNamespace(put=calls.append))
    hub.attach("den", session)
    hub.gateway = SimpleNamespace(_turns={}, _server_turns={})
    return SimpleNamespace(hub=hub, env=env, calls=calls, call=lambda name: json.loads(make_handlers(hub)[name]({})))


def test_tools_hold_the_microphone_from_the_call(tools):
    tools.hub.gateway._turns["den"] = SimpleNamespace(standby=False, mute=False)
    assert tools.call("no_barge_in") == {"result": "ok"}
    assert tools.call("mute") == {"result": "ok"}
    assert tools.call("standby") == {"result": "ok"}
    assert tools.calls == [('ignore', False), ('ignore', True), ('unmute',)]


def test_a_turn_the_server_started_holds_from_its_reply(tools):
    turn = tools.hub.gateway._server_turns["den"] = ServerTurn()
    assert tools.call("no_barge_in") == {"result": "ok"} and turn.no_barge_in
    assert tools.call("mute") == {"result": "ok"} and turn.mute
    assert tools.calls == []                          # nothing of that turn is playing yet
    assert tools.hub.publish("den", "Dinner.", "e1", expires_at=float("inf"), no_barge_in=True) == "accepted"
    assert isinstance(tools.calls[0], ConversationOutput) and tools.calls[0].no_barge_in


@pytest.mark.parametrize('name', ["no_barge_in", "mute"])
def test_tools_report_where_they_cannot_act(tools, name):
    tools.env["HERMES_SESSION_CHAT_ID"] = "elsewhere"
    assert tools.call(name) == {"result": "not_attached"}
    tools.env["HERMES_SESSION_PLATFORM"] = "telegram"
    assert tools.call(name) == {"result": "not_a_device"}


def test_no_barge_in_takes_no_arguments():
    assert SCHEMAS["no_barge_in"]["parameters"] == {"type": "object", "properties": {}}
