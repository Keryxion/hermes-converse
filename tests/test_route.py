"""GET /v1/audio/converse end to end over a real aiohttp TestServer: the plugin route and a scripted
fake of the Runs API share one app, so the loopback key probe, the turn and the stop all go over
real HTTP. STT and the streaming TTS provider are patched at the plugin's speech seam; VAD,
endpointing, capture and the client/driver pipeline run for real."""

from __future__ import annotations

import asyncio
import functools
import json
import wave

import numpy as np
import pytest
from aiohttp import WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer

from hermes_converse import speech
from hermes_converse.loop import voice_system_prompt
from hermes_converse.route import ConverseRoute, loopback_base_url

from tests.support.converse_audio import RMS_NORMAL_SPEECH, RMS_QUIET_SPEECH, room_noise, speech_like
from tests.support.converse_audio import silence as _silence_pcm
from tests.support.fake_api import FakeRunsAPI

API_KEY = "-".join(("fixture", "converse", "api", "key", "0123456789"))
VOICE_PROTOCOL = "hermes-voice-v1"


def _key_protocol(key=API_KEY):
    return f"hermes-key.{key}"


def _start_msg(key=None, **cfg):
    frame = {"type": "start", **cfg}
    if key is not None:
        frame["key"] = key
    return json.dumps(frame)


class _FakeStreamer:
    sample_rate = 24000

    def __init__(self, chunks):
        self.chunks = chunks
        self.requests: list[str] = []

    def stream(self, text):
        self.requests.append(text)
        yield from self.chunks


class Harness:
    """One app serving the fake Runs API and the plugin route; the route's loopback URL is this
    server's own URL once it has started."""

    def __init__(self, fake: FakeRunsAPI, settings: dict | None = None):
        self.fake = fake
        self.settings = settings or {}
        self.base = {"url": ""}
        app = web.Application()
        app.add_routes(fake.routes())
        self.route = ConverseRoute(settings_getter=lambda key, default=None: self.settings.get(key, default),
                      base_url_resolver=lambda: self.base["url"], profile_name="default")
        self.route.wire(app)
        self.app = app

    async def __aenter__(self):
        self.client = await TestClient(TestServer(self.app)).__aenter__()
        self.base["url"] = str(self.client.make_url(""))
        return self.client

    async def __aexit__(self, *exc):
        await self.client.__aexit__(*exc)


def _speech_then_silence_pcm(block=480, speech_blocks=16, silence_blocks=60):
    speech_blk = (np.ones(block, dtype=np.int16) * 9000)
    silence_blk = np.zeros(block, dtype=np.int16)
    frames = [silence_blk.tobytes() for _ in range(20)]
    frames += [speech_blk.tobytes() for _ in range(speech_blocks)]
    frames += [silence_blk.tobytes() for _ in range(silence_blocks)]
    return frames


def _patch_speech(monkeypatch, streamer, *, transcript="hello there"):
    """Fake STT / streaming TTS at the plugin's speech seam so the loop runs end to end offline."""
    monkeypatch.setattr(speech, "streaming_provider", lambda cfg: streamer)
    monkeypatch.setattr(speech, "load_tts_config", dict)
    monkeypatch.setattr(speech, "tts_text_cap", lambda cfg: 4000)
    monkeypatch.setattr(speech, "stt_model_override", lambda: None)
    seen = {"n": 0}

    def _fake_transcribe(wav_path, **kw):
        seen["n"] += 1
        return transcript if seen["n"] == 1 else ""

    monkeypatch.setattr(speech, "transcribe_wav", _fake_transcribe)
    return seen


async def _recv(ws, timeout=6.0):
    return await asyncio.wait_for(ws.receive(), timeout)


async def _collect_turn(ws, timeout=8.0, *, drain=True):
    """Frames until turn_done: (json frames, pcm chunks)."""
    frames, pcm = [], []
    while True:
        msg = await _recv(ws, timeout)
        if msg.type == web.WSMsgType.BINARY:
            pcm.append(msg.data)
            continue
        if msg.type == web.WSMsgType.TEXT:
            payload = json.loads(msg.data)
            frames.append(payload)
            if payload["type"] == "turn_done":
                if drain:
                    await ws.send_json({"type": "drained", "turn_id": payload["turn_id"]})
                break
            continue
        break
    return frames, pcm


async def _drive_full_turn(ws, streamer):
    ready = await ws.receive_json()
    assert ready["type"] == "ready"
    assert ready["input"] == {"sample_rate": 16000, "format": "pcm16", "block_ms": 30}
    assert ready["output"] == {"sample_rate": 24000, "format": "pcm16"}
    assert ready["session_id"]
    assert isinstance(ready["server_recv_unix_ms"], int)
    assert ready["server_send_unix_ms"] >= ready["server_recv_unix_ms"]
    for frame in _speech_then_silence_pcm():
        await ws.send_bytes(frame)
    frames, pcm = await _collect_turn(ws)
    transcript = next(f["text"] for f in frames if f["type"] == "transcript")
    assert transcript == "turn it on"
    assert pcm == [b"\x01\x02\x03\x04", b"\x05\x06"]
    assert streamer.requests
    assert [f["type"] for f in frames] == ["transcript", "thinking", "speaking", "turn_done"]
    assert frames[-1]["next_action"] == "listen"
    return frames


async def _end(ws):
    await ws.send_str(json.dumps({"stop": True}))
    await ws.close()


@pytest.mark.asyncio
async def test_subprotocol_key_accept_full_turn_through_the_runs_api(monkeypatch):
    fake = FakeRunsAPI(API_KEY, deltas=("Turn it ", "on."))
    streamer = _FakeStreamer([b"\x01\x02\x03\x04", b"\x05\x06"])
    _patch_speech(monkeypatch, streamer, transcript="turn it on")
    async with Harness(fake) as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        try:
            assert ws.protocol == VOICE_PROTOCOL  # the key-bearing protocol is never echoed
            await ws.send_str(_start_msg())
            await _drive_full_turn(ws, streamer)
        finally:
            await _end(ws)
    (run,) = fake.runs.values()
    assert run["body"]["input"] == "turn it on"
    assert run["body"]["session_id"].startswith("voice_")
    assert run["body"]["instructions"] == voice_system_prompt("Sakura")
    assert "model" not in run["body"] and "provider" not in run["body"]
    assert run["idempotency_key"].startswith("converse-" + run["body"]["session_id"])
    assert streamer.requests == ["Turn it on."]


@pytest.mark.asyncio
async def test_start_key_accept_full_turn(monkeypatch):
    fake = FakeRunsAPI(API_KEY)
    streamer = _FakeStreamer([b"\x01\x02\x03\x04", b"\x05\x06"])
    _patch_speech(monkeypatch, streamer, transcript="turn it on")
    async with Harness(fake) as client:
        ws = await client.ws_connect("/v1/audio/converse")
        try:
            await ws.send_str(_start_msg(key=API_KEY))
            await _drive_full_turn(ws, streamer)
        finally:
            await _end(ws)


@pytest.mark.asyncio
async def test_session_id_is_stable_across_turns_on_one_socket(monkeypatch):
    fake = FakeRunsAPI(API_KEY, deltas=("ok.",))
    streamer = _FakeStreamer([b"\x01\x02"])
    monkeypatch.setattr(speech, "streaming_provider", lambda cfg: streamer)
    monkeypatch.setattr(speech, "load_tts_config", dict)
    monkeypatch.setattr(speech, "tts_text_cap", lambda cfg: 4000)
    monkeypatch.setattr(speech, "stt_model_override", lambda: None)
    monkeypatch.setattr(speech, "transcribe_wav", lambda path, **kw: "again")
    async with Harness(fake) as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        try:
            await ws.send_str(_start_msg())
            assert (await ws.receive_json())["type"] == "ready"
            for _ in range(2):
                for frame in _speech_then_silence_pcm():
                    await ws.send_bytes(frame)
                await _collect_turn(ws)
        finally:
            await _end(ws)
    sessions = {run["body"]["session_id"] for run in fake.runs.values()}
    assert len(fake.runs) == 2 and len(sessions) == 1


@pytest.mark.asyncio
async def test_settings_drive_the_run_request_and_the_prompt(monkeypatch):
    fake = FakeRunsAPI(API_KEY)
    streamer = _FakeStreamer([b"\x01\x02"])
    _patch_speech(monkeypatch, streamer, transcript="what time is it")
    settings = {"name": "Hal", "voice_model": "fast-model", "voice_provider": "local-llm", "end_phrases": ["that is all"]}
    async with Harness(fake, settings) as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        try:
            await ws.send_str(_start_msg(quiet_interval=5))
            assert (await ws.receive_json())["type"] == "ready"
            for frame in _speech_then_silence_pcm():
                await ws.send_bytes(frame)
            await _collect_turn(ws)
        finally:
            await _end(ws)
    (run,) = fake.runs.values()
    assert run["body"]["model"] == "fast-model" and run["body"]["provider"] == "local-llm"
    assert run["body"]["instructions"] == voice_system_prompt("Hal", allow_signoff=True, end_phrases=("that is all",))
    assert "That is all" in run["body"]["instructions"]


@pytest.mark.asyncio
async def test_start_name_overrides_the_configured_name(monkeypatch):
    fake = FakeRunsAPI(API_KEY)
    _patch_speech(monkeypatch, _FakeStreamer([b"\x01"]), transcript="hi")
    async with Harness(fake, {"name": "Hal"}) as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        try:
            await ws.send_str(_start_msg(name="Nova"))
            assert (await ws.receive_json())["type"] == "ready"
            for frame in _speech_then_silence_pcm():
                await ws.send_bytes(frame)
            await _collect_turn(ws)
        finally:
            await _end(ws)
    (run,) = fake.runs.values()
    assert run["body"]["instructions"] == voice_system_prompt("Nova")


@pytest.mark.asyncio
async def test_start_bad_key_closes_unauthorized():
    async with Harness(FakeRunsAPI(API_KEY)) as client:
        ws = await client.ws_connect("/v1/audio/converse")
        try:
            await ws.send_str(_start_msg(key="nope"))
            msg = await ws.receive()
            assert msg.type == web.WSMsgType.TEXT
            assert json.loads(msg.data) == {"type": "error", "error": "unauthorized"}
            closing = await ws.receive()
            assert closing.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSED)
            assert ws.close_code == 4401
        finally:
            await ws.close()


@pytest.mark.asyncio
async def test_non_start_first_frame_rejected_bad_start():
    async with Harness(FakeRunsAPI(API_KEY)) as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        try:
            await ws.send_str(json.dumps({"type": "auth", "key": API_KEY}))
            msg = await ws.receive()
            assert json.loads(msg.data) == {"type": "error", "error": "bad_start"}
            closing = await ws.receive()
            assert closing.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSED)
            assert ws.close_code == 4400
        finally:
            await ws.close()


@pytest.mark.asyncio
async def test_bad_subprotocol_key_rejects_upgrade_401():
    async with Harness(FakeRunsAPI(API_KEY)) as client:
        with pytest.raises(WSServerHandshakeError) as exc:
            await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol("nope")))
        assert exc.value.status == 401


@pytest.mark.asyncio
async def test_unreachable_api_server_refuses_the_upgrade():
    fake = FakeRunsAPI(API_KEY)
    harness = Harness(fake)
    async with harness as client:
        harness.base["url"] = "http://127.0.0.1:1"  # nothing listens: the probe fails closed
        with pytest.raises(WSServerHandshakeError) as exc:
            await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        assert exc.value.status == 401


@pytest.mark.asyncio
async def test_no_credential_start_frame_closes_unauthorized():
    async with Harness(FakeRunsAPI(API_KEY)) as client:
        ws = await client.ws_connect("/v1/audio/converse")
        try:
            await ws.send_str(_start_msg())
            msg = await ws.receive()
            assert json.loads(msg.data) == {"type": "error", "error": "unauthorized"}
        finally:
            await ws.close()


@pytest.mark.asyncio
async def test_query_token_and_cookie_are_not_credentials():
    async with Harness(FakeRunsAPI(API_KEY)) as client:
        ws = await client.ws_connect(f"/v1/audio/converse?token={API_KEY}",
                                     headers={"Cookie": "session=pretend-valid"})
        try:
            await ws.send_str(_start_msg())
            msg = await ws.receive()
            assert json.loads(msg.data) == {"type": "error", "error": "unauthorized"}
        finally:
            await ws.close()


@pytest.mark.asyncio
async def test_foreign_profile_in_start_frame_is_refused(monkeypatch):
    _patch_speech(monkeypatch, _FakeStreamer([b"\x01"]))
    async with Harness(FakeRunsAPI(API_KEY)) as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        try:
            await ws.send_str(_start_msg(profile="work"))
            msg = await ws.receive()
            assert json.loads(msg.data) == {"type": "error", "error": "profile_unsupported"}
        finally:
            await ws.close()


def _write_tone_wav(path, *, rate=24000, ms=40, freq=440.0):
    n = int(rate * ms / 1000)
    t = np.arange(n, dtype=np.float64) / rate
    samples = (np.sin(2 * np.pi * freq * t) * 12000).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())


@pytest.mark.asyncio
async def test_no_streaming_provider_uses_one_shot_fallback(monkeypatch, tmp_path):
    fake = FakeRunsAPI(API_KEY, deltas=("Sure ", "thing."))
    monkeypatch.setattr(speech, "streaming_provider", lambda cfg: None)
    monkeypatch.setattr(speech, "load_tts_config", dict)
    monkeypatch.setattr(speech, "tts_text_cap", lambda cfg: 4000)
    monkeypatch.setattr(speech, "stt_model_override", lambda: None)
    src = tmp_path / "reply.wav"
    _write_tone_wav(src)
    call = {"n": 0}

    def _fake_tts(text):
        call["n"] += 1
        dst = tmp_path / f"reply-{call['n']}.wav"
        dst.write_bytes(src.read_bytes())
        return json.dumps({"success": True, "file_path": str(dst)})

    monkeypatch.setattr(speech, "text_to_speech", _fake_tts)
    seen = {"n": 0}

    def _fake_transcribe(wav_path, **kw):
        seen["n"] += 1
        return "hello there" if seen["n"] == 1 else ""

    monkeypatch.setattr(speech, "transcribe_wav", _fake_transcribe)
    async with Harness(fake) as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        try:
            await ws.send_str(_start_msg())
            ready = await ws.receive_json()
            assert ready["output"] == {"sample_rate": 24000, "format": "pcm16"}
            for frame in _speech_then_silence_pcm():
                await ws.send_bytes(frame)
            _, pcm = await _collect_turn(ws)
            assert pcm and b"".join(pcm)
        finally:
            await _end(ws)


@pytest.mark.asyncio
async def test_equal_input_output_rates_reported_and_full_turn(monkeypatch):
    fake = FakeRunsAPI(API_KEY)
    streamer = _FakeStreamer([b"\x01\x02\x03\x04", b"\x05\x06"])
    streamer.sample_rate = 16000
    _patch_speech(monkeypatch, streamer, transcript="turn it on")
    async with Harness(fake) as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        try:
            await ws.send_str(_start_msg(input_rate=16000, output_rate=16000))
            ready = await ws.receive_json()
            assert ready["input"] == {"sample_rate": 16000, "format": "pcm16", "block_ms": 30}
            assert ready["output"] == {"sample_rate": 16000, "format": "pcm16"}
            for frame in _speech_then_silence_pcm():
                await ws.send_bytes(frame)
            frames, pcm = await _collect_turn(ws)
            assert next(f["text"] for f in frames if f["type"] == "transcript") == "turn it on"
            assert pcm == [b"\x01\x02\x03\x04", b"\x05\x06"]  # no-op resample: bytes unchanged
        finally:
            await _end(ws)


# ── realistic audio over the socket ──

def _pcm_frames(arr, block=480):
    return [arr[i:i + block].tobytes() for i in range(0, len(arr) - 1, block)]


def _calibration_frames(block=480, blocks=40):
    rng = np.random.default_rng(7)
    calib = (rng.standard_normal(block * blocks) * 50).clip(-32000, 32000).astype(np.int16)
    return _pcm_frames(calib, block)


@pytest.mark.asyncio
async def test_quiet_speech_full_turn_over_ws(monkeypatch):
    fake = FakeRunsAPI(API_KEY)
    streamer = _FakeStreamer([b"\x01\x02\x03\x04"])
    _patch_speech(monkeypatch, streamer, transcript="quiet please")
    async with Harness(fake) as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        try:
            await ws.send_str(_start_msg())
            assert json.loads((await _recv(ws)).data)["type"] == "ready"
            for f in _calibration_frames():
                await ws.send_bytes(f)
            for f in _pcm_frames(speech_like(RMS_QUIET_SPEECH, seed=500)):
                await ws.send_bytes(f)
            for f in _pcm_frames(_silence_pcm(seconds=1.7)):
                await ws.send_bytes(f)
            frames, pcm = await _collect_turn(ws)
            got = next((f["text"] for f in frames if f["type"] == "transcript"), None)
            assert got == "quiet please", f"soft voice was not heard: {got!r}"
            assert pcm
        finally:
            await _end(ws)


@pytest.mark.asyncio
async def test_room_noise_never_becomes_a_phantom_turn(monkeypatch):
    fake = FakeRunsAPI(API_KEY)
    _patch_speech(monkeypatch, _FakeStreamer([b"\x09\x09"]), transcript="the real one")
    async with Harness(fake) as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        try:
            await ws.send_str(_start_msg())
            assert json.loads((await _recv(ws)).data)["type"] == "ready"
            for f in _calibration_frames():
                await ws.send_bytes(f)
            for f in _pcm_frames(room_noise(200, seconds=1.5, seed=13)):
                await ws.send_bytes(f)
            for f in _pcm_frames(speech_like(RMS_NORMAL_SPEECH, seed=1)):
                await ws.send_bytes(f)
            for f in _pcm_frames(_silence_pcm(seconds=1.7)):
                await ws.send_bytes(f)
            frames, _ = await _collect_turn(ws)
            assert [f["text"] for f in frames if f["type"] == "transcript"] == ["the real one"]
        finally:
            await _end(ws)


@pytest.mark.asyncio
async def test_socket_stays_open_across_quiet_then_takes_a_turn(monkeypatch):
    fake = FakeRunsAPI(API_KEY)
    _patch_speech(monkeypatch, _FakeStreamer([b"\x01\x02"]), transcript="still here")
    async with Harness(fake) as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        try:
            await ws.send_str(_start_msg(quiet_interval=1))
            assert json.loads((await _recv(ws)).data)["type"] == "ready"
            for f in _calibration_frames(blocks=30):
                await ws.send_bytes(f)
            for f in _pcm_frames(_silence_pcm(seconds=2.5)):
                await ws.send_bytes(f)
            saw_quiet = False
            for _ in range(300):
                msg = await _recv(ws, timeout=6.0)
                if msg.type == web.WSMsgType.TEXT and json.loads(msg.data).get("type") == "quiet":
                    saw_quiet = True
                    break
                if msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSED, web.WSMsgType.ERROR):
                    break
            assert saw_quiet and not ws.closed
            for f in _pcm_frames(speech_like(RMS_NORMAL_SPEECH, seed=9)):
                await ws.send_bytes(f)
            for f in _pcm_frames(_silence_pcm(seconds=1.7)):
                await ws.send_bytes(f)
            frames, _ = await _collect_turn(ws)
            assert next(f["text"] for f in frames if f["type"] == "transcript") == "still here"
        finally:
            await _end(ws)


@pytest.mark.asyncio
async def test_no_audio_sent_yields_no_quiet_ping(monkeypatch):
    fake = FakeRunsAPI(API_KEY)
    _patch_speech(monkeypatch, _FakeStreamer([b"\x01"]), transcript="unused")
    async with Harness(fake) as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        try:
            await ws.send_str(_start_msg(quiet_interval=1))
            assert json.loads((await _recv(ws)).data)["type"] == "ready"
            with pytest.raises(asyncio.TimeoutError):
                await _recv(ws, timeout=2.5)
            assert not ws.closed
        finally:
            await _end(ws)


# ── the run is never orphaned ──

@pytest.mark.asyncio
async def test_barge_in_stops_the_live_run(monkeypatch):
    """The user talks over the reply: playback is cut, the plugin POSTs /stop, the turn ends with
    `interrupted` + `turn_done`, and the socket is still open for the next utterance."""
    fake = FakeRunsAPI(API_KEY, deltas=("Here is a long answer that keeps going. ",), hang=True)
    streamer = _FakeStreamer([b"\x01\x02\x03\x04"])
    _patch_speech(monkeypatch, streamer, transcript="tell me everything")
    async with Harness(fake) as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        try:
            await ws.send_str(_start_msg())
            assert (await ws.receive_json())["type"] == "ready"
            for frame in _speech_then_silence_pcm():
                await ws.send_bytes(frame)
            # Wait for playback to start (the first PCM chunk), then keep talking over it.
            while True:
                msg = await _recv(ws)
                if msg.type == web.WSMsgType.BINARY:
                    break
            loud = (np.ones(480, dtype=np.int16) * 9000).tobytes()
            for _ in range(60):  # past the 500 ms playback grace, sustained well over the clamp
                await ws.send_bytes(loud)
            for _ in range(60):
                await ws.send_bytes(np.zeros(480, dtype=np.int16).tobytes())
            frames, _ = await _collect_turn(ws, timeout=10.0)
            types = [f["type"] for f in frames]
            assert "interrupted" in types and types[-1] == "turn_done"
            assert not ws.closed
        finally:
            await _end(ws)
    run_id = next(iter(fake.runs))
    assert fake.stop_calls[0] == run_id and fake.runs[run_id]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_closing_the_socket_mid_turn_stops_the_run(monkeypatch):
    fake = FakeRunsAPI(API_KEY, deltas=("Thinking out loud. ",), hang=True)
    _patch_speech(monkeypatch, _FakeStreamer([b"\x01\x02"]), transcript="go")
    async with Harness(fake) as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        await ws.send_str(_start_msg())
        assert (await ws.receive_json())["type"] == "ready"
        for frame in _speech_then_silence_pcm():
            await ws.send_bytes(frame)
        while True:  # the run is live once its first audio is back
            msg = await _recv(ws)
            if msg.type == web.WSMsgType.BINARY:
                break
        await ws.close()
        run_id = next(iter(fake.runs))
        for _ in range(100):  # while the server is still up: the close itself must stop the run
            if fake.runs[run_id]["status"] == "cancelled":
                break
            await asyncio.sleep(0.05)
        assert fake.stop_calls == [run_id]
        assert fake.runs[run_id]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_run_refusal_is_spoken_as_an_error_frame(monkeypatch):
    """A 409 from /v1/runs (a session another surface owns, a draining profile) reaches the client
    as an error frame followed by turn_done — never a hung socket."""
    fake = FakeRunsAPI(API_KEY)

    async def _refuse(request):
        return web.json_response({"error": {"message": "session is owned by another surface",
                                            "code": "permission_denied"}}, status=409)

    fake.create_run = _refuse
    _patch_speech(monkeypatch, _FakeStreamer([b"\x01"]), transcript="hello")
    async with Harness(fake) as client:
        ws = await client.ws_connect("/v1/audio/converse", protocols=(VOICE_PROTOCOL, _key_protocol()))
        try:
            await ws.send_str(_start_msg())
            assert (await ws.receive_json())["type"] == "ready"
            for frame in _speech_then_silence_pcm():
                await ws.send_bytes(frame)
            frames, pcm = await _collect_turn(ws)
            errors = [f for f in frames if f["type"] == "error"]
            assert errors and "409" in errors[0]["error"] and "another surface" in errors[0]["error"]
            assert pcm == [] and frames[-1]["type"] == "turn_done"
            assert frames[-1]["next_action"] == "listen"
        finally:
            await _end(ws)


def test_loopback_base_url_prefers_the_setting_then_the_listener():
    class _Adapter:
        _host, _port = "0.0.0.0", 9000

    assert loopback_base_url(_Adapter(), "http://127.0.0.1:1234/") == "http://127.0.0.1:1234"
    assert loopback_base_url(_Adapter()) == "http://127.0.0.1:9000"
    _Adapter._host = "::"
    assert loopback_base_url(_Adapter()) == "http://[::1]:9000"
    _Adapter._host = "192.168.1.5"
    assert loopback_base_url(_Adapter()) == "http://192.168.1.5:9000"
    assert loopback_base_url(object()) == "http://127.0.0.1:8642"


def test_wire_on_a_frozen_router_warns_instead_of_raising(caplog):
    """Enabling the plugin while the gateway runs reaches the factory after AppRunner.setup() froze
    the router: that must be a clear warning (restart to register), never a traceback."""
    app = web.Application()
    app.router.freeze()
    route = ConverseRoute(settings_getter=lambda key, default=None: default, base_url_resolver=lambda: "")
    with caplog.at_level("WARNING"):
        assert route.wire(app) is False
    assert "restart the gateway" in caplog.text
    assert not any(r.resource.canonical == "/v1/audio/converse" for r in app.router.routes())
