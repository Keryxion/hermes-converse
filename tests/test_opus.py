"""Real libopus interoperability and socket lifecycle regressions."""

import asyncio
import ctypes as ct

import numpy as np
import pytest
from aiohttp import web

from hermes_converse.audio_transport import AudioTransport, audio_format
from hermes_converse.catchup import Relay
from hermes_converse.opus import Decoder, Encoder, OpusError, RATES
from tests.support.converse_audio import calibration, silence, speech_like
from tests.support.fake_hermes import FakeHermes
from tests.test_route import API_KEY, Harness, _FakeStreamer, _collect_turn, _patch_speech, _start_msg


@pytest.mark.parametrize("rate", RATES)
def test_real_codec_rates_settings_and_dtx(rate):
    encoder, decoder = Encoder(rate), Decoder(rate)
    try:
        # Query the native encoder, rather than mirroring the wrapper's constants.
        for request, expected in ((4001, 2048), (4003, 24000), (4011, 5), (4017, 1)):
            value = ct.c_int()
            assert encoder.lib.opus_encoder_ctl(encoder.state, request, ct.byref(value)) == 0
            assert value.value == expected
        pcm = speech_like(3000, seconds=0.02, rate=rate).astype("<i2").tobytes()
        decoded = decoder.decode(encoder.encode(pcm))
        assert len(decoded) == rate // 50 * 2
        assert np.abs(np.frombuffer(decoded, dtype="<i2")).max() > 100
        packets = [encoder.encode(bytes(len(pcm))) for _ in range(100)]
        assert any(len(p) <= 2 for p in packets)
        assert all(len(decoder.decode(p)) == len(pcm) for p in packets)
    finally:
        encoder.close()
        decoder.close()


@pytest.mark.parametrize("frame", [
    {"audio_format": "mp3"}, {"audio_format": None}, {"audio_format": []},
    *({"audio_format": "opus", key: value} for key in ("input_rate", "output_rate")
      for value in (0, 7999, 44100, 48001, "16000", 16000.0, None, True)),
])
def test_invalid_start(frame):
    with pytest.raises(ValueError):
        audio_format(frame)


def test_pcm_default_and_codec_errors():
    assert audio_format({}) == "pcm16"
    assert audio_format({"audio_format": "pcm16", "input_rate": 44100}) == "pcm16"
    decoder = Decoder(16000)
    try:
        for packet in (b"", b"\xff", bytes(1276), b"\x00"):
            with pytest.raises(OpusError):
                decoder.decode(packet)
    finally:
        decoder.close()
    with pytest.raises(OpusError):
        decoder.decode(b"\x48")


def test_pcm_does_not_load_libopus(monkeypatch):
    def missing():
        raise OSError("libopus missing")

    monkeypatch.setattr("hermes_converse.opus._library", missing)
    audio = AudioTransport("pcm16", 16000, 24000)
    assert audio.decode(b"\x01") == b"\x01"
    assert list(audio.encode(b"\x01\x02\x03", 24000)) == [b"\x01\x02\x03"]
    assert list(audio.finish()) == []
    audio.close()
    with pytest.raises(OSError, match="libopus missing"):
        AudioTransport("opus", 16000, 24000)


def test_dtx_packets_advance_audio_without_learning_synthetic_noise():
    audio = AudioTransport("opus", 16000, 16000)
    encoder = Encoder(16000)
    try:
        packets = [encoder.encode(bytes(640)) for _ in range(100)]
        tiny = [p for p in packets if len(p) <= 2]
        assert tiny
        assert all(audio.decode(p) == bytes(640) for p in tiny)
    finally:
        audio.close()
        encoder.close()


def test_odd_chunks_partial_frame_and_encoder_delay_preserve_tail():
    audio = AudioTransport("opus", 16000, 24000)
    decoder = Decoder(24000)
    # A pulse near the very end exercises both final partial-frame padding and codec delay.
    pcm = np.zeros(1001, dtype="<i2")
    pcm[-100:] = (np.sin(np.arange(100) * 2 * np.pi * 1000 / 24000) * 12000).astype("<i2")
    raw = pcm.tobytes()
    try:
        packets = []
        for start in range(0, len(raw), 137):
            packets.extend(audio.encode(raw[start:start + 137], 24000))
        packets.extend(audio.finish())
        decoded = np.frombuffer(b"".join(decoder.decode(p) for p in packets), dtype="<i2")
        assert len(decoded) % 480 == 0
        assert len(decoded) >= len(pcm) + audio.encoder.lookahead
        assert len(decoded) < len(pcm) + audio.encoder.lookahead + 480
        assert np.abs(decoded[len(pcm):]).max() > 1000  # tail arrived after the codec delay
        assert list(audio.finish()) == []
    finally:
        audio.close()
        decoder.close()


class Socket:
    def __init__(self):
        self.frames = []

    async def send_bytes(self, data):
        self.frames.append(data)

    async def send_json(self, frame):
        self.frames.append(frame)


@pytest.mark.asyncio
async def test_relay_cancellation_drops_pending_audio_and_next_turn_has_fresh_state():
    audio = AudioTransport("opus", 16000, 24000)
    ws = Socket()
    relay = Relay(ws, audio=audio, source_rate=24000)
    try:
        await relay.send_json({"type": "speaking", "turn_id": "one"})
        await relay.send_bytes(bytes(200))  # under one packet: never sent
        await relay.send_json({"type": "interrupted", "turn_id": "one"})
        await relay.send_json({"type": "turn_done", "turn_id": "one"})
        assert not any(isinstance(f, bytes) for f in ws.frames)
        await relay.send_json({"type": "speaking", "turn_id": "two"})
        await relay.send_bytes(bytes(1000))
        await relay.send_json({"type": "turn_done", "turn_id": "two"})
        assert isinstance(ws.frames[-2], bytes)
        assert ws.frames[-1]["type"] == "turn_done"
    finally:
        audio.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_format,new_format", [("pcm16", "opus"), ("opus", "pcm16"), ("opus", "opus")])
async def test_relay_reconnect_changes_format_and_resamples_running_turn(old_format, new_format):
    old = AudioTransport(old_format, 16000, 16000)
    new = AudioTransport(new_format, 16000, 24000)
    ws = Socket()
    relay = Relay(Socket(), audio=old, source_rate=16000)
    try:
        await relay.send_bytes(bytes(640))
        relay.ws = None
        await relay.send_bytes(bytes(640))
        relay.attach(ws, audio=new)
        pcm = speech_like(2000, seconds=0.1).astype("<i2").tobytes()
        for start in range(0, len(pcm), 137):
            await relay.send_bytes(pcm[start:start + 137])
        await relay.send_json({"type": "turn_done", "turn_id": "one"})
        packets = [f for f in ws.frames if isinstance(f, bytes)]
        if new_format == "opus":
            decoder = Decoder(24000)
            try:
                samples = b"".join(decoder.decode(p) for p in packets)
            finally:
                decoder.close()
            assert 4800 <= len(samples) < 6720
        else:
            assert len(b"".join(packets)) == 4800
    finally:
        old.close()
        new.close()


@pytest.mark.asyncio
async def test_opus_socket_full_turn_and_dtx_quiet(monkeypatch):
    fake = FakeHermes(API_KEY)
    streamer = _FakeStreamer([speech_like(2500, seconds=0.15, rate=24000).tobytes()])
    seen = _patch_speech(monkeypatch, streamer)
    encoder, decoder = Encoder(16000), Decoder(24000)
    async with Harness(fake, {"vad_model": "energy"}) as client:
        ws = await client.ws_connect("/v1/audio/converse")
        try:
            await ws.send_str(_start_msg(key=API_KEY, audio_format="opus", quiet_interval=1))
            ready = await ws.receive_json()
            assert ready["input"] == {"sample_rate": 16000, "format": "opus", "block_ms": 20}
            assert ready["output"] == {"sample_rate": 24000, "format": "opus", "block_ms": 20}
            pcm = np.concatenate([calibration(), speech_like(4000), silence(2.4)]).astype("<i2").tobytes()
            for start in range(0, len(pcm), 640):
                await ws.send_bytes(encoder.encode(pcm[start:start + 640].ljust(640, b"\0")))
            frames, packets = await _collect_turn(ws)
            assert frames[-1]["type"] == "turn_done"
            assert seen["n"] == 1 and len(fake.turns) == 1
            decoded = b"".join(decoder.decode(p) for p in packets)
            assert np.abs(np.frombuffer(decoded, dtype="<i2")).max() > 100
            # Wait until the worker consumes the post-request audio, then send only DTX.
            await asyncio.sleep(0.1)
            for _ in range(100):
                await ws.send_bytes(encoder.encode(bytes(640)))
            while True:
                frame = await ws.receive_json(timeout=3)
                if frame["type"] == "quiet":
                    assert frame["quiet_seconds"] >= 1
                    break
        finally:
            await ws.close()
            encoder.close()
            decoder.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["pcm16", "opus"])
async def test_listening_mute_sends_captured_audio_then_commit_without_silence(monkeypatch, fmt):
    fake = FakeHermes(API_KEY)
    _patch_speech(monkeypatch, _FakeStreamer([bytes(960)]))
    audio = AudioTransport(fmt, 16000, 16000)
    try:
        harness = Harness(fake, {"vad_model": "energy"})
        async with harness as client:
            ws = await client.ws_connect("/v1/audio/converse")
            await ws.send_str(_start_msg(key=API_KEY, audio_format=fmt))
            assert (await ws.receive_json())["type"] == "ready"
            await ws.send_json({"commit": True})
            await asyncio.sleep(0.1)
            assert not fake.turns  # no utterance to commit
            pcm = np.concatenate([calibration(), speech_like(4000, seconds=1)]).astype("<i2").tobytes()
            for packet in audio.encode(pcm, 16000):
                await ws.send_bytes(packet)
            for packet in audio.finish():
                await ws.send_bytes(packet)
            await ws.send_json({"commit": True})
            frames, packets = await _collect_turn(ws)
            assert frames[-1]["type"] == "turn_done" and packets
            assert len(fake.turns) == 1 and fake.turns[0]["text"] == "hello there"
            await ws.close()
    finally:
        audio.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [{"audio_format": "unknown"}, {"audio_format": "opus", "input_rate": 44100}])
async def test_bad_audio_start_closes_before_binding_conversation(cfg):
    fake = FakeHermes(API_KEY)
    async with Harness(fake) as client:
        ws = await client.ws_connect("/v1/audio/converse")
        await ws.send_str(_start_msg(key=API_KEY, **cfg))
        assert (await ws.receive_json())["error"] == "bad_start"
        assert (await ws.receive()).data == 4400
        assert not fake.store.ids


@pytest.mark.asyncio
async def test_bad_opus_packet_closes_socket(monkeypatch):
    _patch_speech(monkeypatch, _FakeStreamer([]))
    async with Harness(FakeHermes(API_KEY)) as client:
        ws = await client.ws_connect("/v1/audio/converse")
        await ws.send_str(_start_msg(key=API_KEY, audio_format="opus"))
        assert (await ws.receive_json())["type"] == "ready"
        await ws.send_bytes(b"not an opus packet")
        assert (await ws.receive_json())["error"] == "bad_audio"
        assert (await ws.receive()).type == web.WSMsgType.CLOSE
        assert ws.close_code == 4400


@pytest.mark.asyncio
@pytest.mark.parametrize("old_format,new_format", [("opus", "pcm16"), ("pcm16", "opus")])
async def test_running_turn_reconnects_with_new_wire_format(monkeypatch, old_format, new_format):
    entered, resume = asyncio.Event(), asyncio.Event()

    class DelayedFake(FakeHermes):
        async def handle(self, event):
            entered.set()
            await resume.wait()
            return await super().handle(event)

    fake = DelayedFake(API_KEY)
    _patch_speech(monkeypatch, _FakeStreamer([speech_like(2500, seconds=0.1, rate=24000).tobytes()]))
    source = AudioTransport(old_format, 16000, 16000)
    try:
        async with Harness(fake, {"vad_model": "energy"}) as client:
            first = await client.ws_connect("/v1/audio/converse")
            await first.send_str(_start_msg(key=API_KEY, audio_format=old_format, output_rate=16000))
            ready = await first.receive_json()
            pcm = np.concatenate([calibration(), speech_like(4000), silence(2.4)]).astype("<i2").tobytes()
            for packet in source.encode(pcm, 16000):
                await first.send_bytes(packet)
            await asyncio.wait_for(entered.wait(), 3)
            await first.close()
            second = await client.ws_connect("/v1/audio/converse")
            await second.send_str(_start_msg(key=API_KEY, audio_format=new_format, output_rate=24000))
            ready2 = await second.receive_json()
            assert ready2["type"] == "ready", ready2
            assert ready2["recipient_id"] == ready["recipient_id"]
            resume.set()
            frames, packets = await _collect_turn(second)
            assert frames[-1]["type"] == "turn_done" and packets
            if new_format == "opus":
                decoder = Decoder(24000)
                try:
                    decoded = b"".join(decoder.decode(p) for p in packets)
                finally:
                    decoder.close()
            else:
                decoded = b"".join(packets)
            assert 4500 <= len(decoded) <= 6720
            await second.close()
    finally:
        resume.set()
        source.close()
