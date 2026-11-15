"""Per-socket Opus state around the existing PCM voice pipeline."""

from __future__ import annotations

from .opus import Decoder, Encoder, RATES


def audio_format(frame: dict) -> str:
    fmt = frame.get("audio_format", "pcm16")
    if fmt not in ("pcm16", "opus"):
        raise ValueError("bad_start")
    if fmt == "opus":
        for key, default in (("input_rate", 16000), ("output_rate", 24000)):
            rate = frame.get(key, default)
            if type(rate) is not int or rate not in RATES:
                raise ValueError("bad_start")
    return fmt


class AudioTransport:
    def __init__(self, fmt: str, input_rate: int, output_rate: int):
        self.output_rate = output_rate
        self.decoder = Decoder(input_rate) if fmt == "opus" else None
        try:
            self.encoder = Encoder(output_rate) if fmt == "opus" else None
        except BaseException:
            if self.decoder is not None:
                self.decoder.close()
            raise
        self.pending = bytearray()
        self.active = False
        self.resampler = None
        self.source_carry = b""
        self.turn_id = None

    def begin(self, turn_id):
        if turn_id != self.turn_id:
            self.discard()
            if self.encoder is not None:
                rate = self.encoder.rate
                self.encoder.close()
                self.encoder = Encoder(rate)
            self.turn_id = turn_id

    def close(self):
        if self.decoder is not None:
            self.decoder.close()
            self.encoder.close()

    def decode(self, packet: bytes) -> bytes:
        if self.decoder is None:
            return packet
        pcm = self.decoder.decode(packet)
        # Still decode DTX-only packets to maintain codec state, but don't let generated
        # comfort noise teach the room gain or trigger speech detection.
        return bytes(len(pcm)) if len(packet) <= 2 else pcm

    def encode(self, pcm: bytes, source_rate: int):
        if self.encoder is not None:
            self.active = self.active or bool(pcm)
        if source_rate != self.output_rate:
            import av
            import numpy as np
            if self.resampler is None:
                self.resampler = av.AudioResampler(format="s16", layout="mono", rate=self.output_rate)
            pcm = self.source_carry + pcm
            cut = len(pcm) - len(pcm) % 2
            pcm, self.source_carry = pcm[:cut], pcm[cut:]
            if not pcm:
                return
            frame = av.AudioFrame.from_ndarray(np.frombuffer(pcm, dtype="<i2").reshape(1, -1),
                                               format="s16", layout="mono")
            frame.sample_rate = source_rate
            pcm = b"".join(f.to_ndarray().tobytes() for f in self.resampler.resample(frame))
        if self.encoder is None:
            if pcm:
                yield pcm
            return
        self.pending.extend(pcm)
        yield from self._packets()

    def _packets(self):
        size = self.encoder.frame_bytes
        while len(self.pending) >= size:
            pcm = bytes(self.pending[:size])
            del self.pending[:size]
            yield self.encoder.encode(pcm)

    def finish(self):
        if self.resampler is not None:
            pcm = b"".join(f.to_ndarray().tobytes() for f in self.resampler.resample(None))
            self.resampler = None
            if self.encoder is None:
                if pcm:
                    yield pcm
            else:
                self.pending.extend(pcm)
        if self.active:
            self.pending.extend(bytes(self.encoder.lookahead * 2))
            remainder = len(self.pending) % self.encoder.frame_bytes
            if remainder:
                self.pending.extend(bytes(self.encoder.frame_bytes - remainder))
            yield from self._packets()
        self.active = False
        self.source_carry = b""

    def discard(self):
        self.pending.clear()
        self.resampler = None
        self.active = False
        self.source_carry = b""
