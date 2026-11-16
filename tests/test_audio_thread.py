"""Connections and turns add no threads: every session's audio runs as tasks on the route's loop,
its CPU work on the one shared audio thread, and STT and TTS as short calls on a default pool
here held to one worker."""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from aiohttp import web

from hermes_converse import speech
from tests.support.fake_hermes import FakeHermes
from tests.test_route import API_KEY, Harness, _collect_turn, _end, _recv, _speech_then_silence_pcm, _start_msg

SESSIONS = 20
SILENCE = np.zeros(480, dtype=np.int16).tobytes()
LOUD = (np.ones(480, dtype=np.int16) * 9000).tobytes()


class _Streamer:
    sample_rate = 24000

    def __init__(self, chunks: int) -> None:
        self.chunks = chunks

    def stream(self, text):
        for _ in range(self.chunks):
            yield bytes(4800)  # 100 ms


def _patch(monkeypatch, streamer) -> None:
    monkeypatch.setattr(speech, "streaming_provider", lambda cfg: streamer)
    monkeypatch.setattr(speech, "load_tts_config", dict)
    monkeypatch.setattr(speech, "tts_text_cap", lambda cfg: 4000)
    monkeypatch.setattr(speech, "stt_model_override", lambda: None)
    monkeypatch.setattr(speech, "transcribe_wav", lambda path, **kw: "hello there")


async def _connect(client, name: str):
    ws = await client.ws_connect("/v1/audio/converse")
    await ws.send_str(_start_msg(key=API_KEY, device_name=name))
    assert (await ws.receive_json(timeout=10))["type"] == "ready"
    return ws


async def _talk(ws) -> tuple[list, list]:
    """One request in real time, then the turn it starts."""
    for frame in _speech_then_silence_pcm():
        await ws.send_bytes(frame)
        await asyncio.sleep(0.03)
    return await _collect_turn(ws, timeout=30.0)


class _ThreadCount:
    """The most threads alive at once while it runs."""

    def __init__(self) -> None:
        self.most = threading.active_count()

    async def watch(self) -> None:
        while True:
            self.most = max(self.most, threading.active_count())
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_concurrent_sessions_and_turns_add_no_threads(monkeypatch):
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
    _patch(monkeypatch, _Streamer(3))
    async with Harness(FakeHermes(API_KEY)) as client:
        # A first exchange starts the audio thread and the default pool's one worker.
        first = await _connect(client, "Warm Up")
        frames, pcm = await _talk(first)
        assert frames[-1]["type"] == "turn_done" and pcm
        baseline = threading.active_count()
        count = _ThreadCount()
        watcher = asyncio.create_task(count.watch())
        sockets = [await _connect(client, f"Room {i}") for i in range(SESSIONS)]
        try:
            turns = await asyncio.gather(*(_talk(ws) for ws in sockets))
        finally:
            watcher.cancel()
            for ws in [first, *sockets]:
                await ws.close()
        for frames, pcm in turns:
            types = [f["type"] for f in frames]
            assert "speaking" in types and types[-1] == "turn_done" and pcm
        assert count.most <= baseline + 2, (baseline, count.most)


@pytest.mark.asyncio
async def test_barge_in_with_one_pool_worker(monkeypatch):
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
    _patch(monkeypatch, _Streamer(100))  # ten seconds of reply
    fake = FakeHermes(API_KEY, deltas=("Here is a long answer that keeps going. ",), hang=True)
    async with Harness(fake) as client:
        ws = await _connect(client, "Kitchen")
        try:
            for frame in _speech_then_silence_pcm():
                await ws.send_bytes(frame)
            while (await _recv(ws, 10.0)).type != web.WSMsgType.BINARY:
                pass  # playback has started
            for _ in range(60):  # talk over it
                await ws.send_bytes(LOUD)
                await asyncio.sleep(0.01)
            for _ in range(60):
                await ws.send_bytes(SILENCE)
            frames, _ = await _collect_turn(ws, timeout=10.0)
            types = [f["type"] for f in frames]
            assert "interrupted" in types and types[-1] == "turn_done"
            assert not ws.closed
        finally:
            await _end(ws)
    assert fake.turns[0]["status"] == "interrupted"
