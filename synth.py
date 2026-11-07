"""Converse synthesizer: one uniform ``text -> int16 PCM`` seam whichever TTS provider is configured.

When the configured provider has a chunked/streaming API we use it (low latency: playback starts on
sentence one); when it doesn't (edge, the default) we fall back to one-shot synthesis of the whole
sentence and transcode the resulting audio file to raw int16 PCM server-side. Both expose the same
``.sample_rate: int`` + ``.synth(text) -> Iterator[bytes]`` contract, and :func:`resample_synth`
wraps either so a single-clock device gets its own output rate.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator

from . import speech
from .vad import unlink_quietly

logger = logging.getLogger(__name__)

# One-shot fallback synthesis always decodes to this rate (matches the built-in streamers' 24 kHz so
# the wire format — and the `ready` frame's output.sample_rate — is identical whichever path serves
# a turn).
FALLBACK_SAMPLE_RATE = 24000
# Split fallback PCM into ~32 KiB frames so a long sentence doesn't land as one giant WS frame.
FALLBACK_PCM_CHUNK_BYTES = 32 * 1024


def decode_audio_file_to_pcm16(path: str, target_rate: int = FALLBACK_SAMPLE_RATE) -> bytes:
    """Decode an audio file to raw little-endian int16 mono PCM at *target_rate* with PyAV (any
    container the one-shot providers emit: mp3, wav, opus/ogg, …). On any failure logs and returns
    ``b""`` so a bad file degrades to "no audio", never an exception into the synthesis thread."""
    try:
        import av
        import av.audio.resampler

        resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=target_rate)
        out = bytearray()

        def _emit(frame) -> None:
            # resample() returns a LIST of frames (may be empty). to_ndarray() gives the exact
            # sample count; the raw plane buffer is over-allocated and would append garbage.
            for rs in resampler.resample(frame):
                data = rs.to_ndarray().tobytes()
                if data:
                    out.extend(data)

        with av.open(path) as container:
            for frame in container.decode(audio=0):
                _emit(frame)
        _emit(None)  # flush the resampler's internal buffer
        return bytes(out)
    except Exception:
        logger.warning("converse fallback: failed to decode %s", path, exc_info=True)
        return b""


class StreamingSynth:
    """Adapter over a streaming TTS provider (the low-latency path)."""

    def __init__(self, streamer: Any) -> None:
        self._streamer = streamer
        self.sample_rate: int = int(streamer.sample_rate)

    def synth(self, text: str) -> Iterator[bytes]:
        return speech.pcm_chunks(self._streamer, text)


class OneShotSynth:
    """One-shot fallback: synth to a temp file, transcode to int16 PCM, yield it. Works with ANY
    provider (including edge, which has no chunked API). A provider that reports failure or writes
    no readable file yields nothing (the loop treats that as a silent turn)."""

    sample_rate: int = FALLBACK_SAMPLE_RATE

    def synth(self, text: str) -> Iterator[bytes]:
        result_json = speech.text_to_speech(text)
        try:
            result = json.loads(result_json) if isinstance(result_json, str) else result_json
        except ValueError:
            logger.debug("converse fallback: TTS envelope was not valid JSON")
            return
        if not isinstance(result, dict) or not result.get("success"):
            logger.debug("converse fallback: TTS reported no audio (%s)",
                         result.get("error") if isinstance(result, dict) else result)
            return
        file_path = result.get("file_path")
        if not file_path:
            logger.debug("converse fallback: TTS envelope had no file_path")
            return
        try:
            pcm = decode_audio_file_to_pcm16(file_path, self.sample_rate)
        finally:
            unlink_quietly(file_path)
        for start in range(0, len(pcm), FALLBACK_PCM_CHUNK_BYTES):
            yield pcm[start:start + FALLBACK_PCM_CHUNK_BYTES]


def resolve_synthesizer(tts_config: dict) -> Any:
    """A synthesizer for the converse loop — NEVER ``None``: the configured streaming provider when
    it has a chunked API, else the one-shot fallback."""
    streamer = speech.streaming_provider(tts_config)
    if streamer is not None:
        return StreamingSynth(streamer)
    return OneShotSynth()


class ResampledSynth:
    """Wrap a synth so ``.synth(text)`` yields int16 mono PCM at *output_rate*: each chunk goes
    through a stateful PyAV resampler, flushed at end-of-text. A fresh resampler per ``synth`` call
    keeps turns independent."""

    def __init__(self, inner: Any, output_rate: int) -> None:
        self._inner = inner
        self.sample_rate: int = int(output_rate)
        self._src_rate: int = int(inner.sample_rate)

    def synth(self, text: str) -> Iterator[bytes]:
        import av
        import av.audio.resampler
        import numpy as np

        resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=self.sample_rate)

        def _feed(frame) -> Iterator[bytes]:
            for rs in resampler.resample(frame):
                data = rs.to_ndarray().tobytes()
                if data:
                    yield data

        carry = b""
        for chunk in self._inner.synth(text):
            if not chunk:
                continue
            carry += chunk
            n = len(carry) - (len(carry) % 2)  # feed whole int16 samples only
            if n == 0:
                continue
            buf, carry = carry[:n], carry[n:]
            arr = np.frombuffer(buf, dtype=np.int16).reshape(1, -1)  # (channels, samples)
            frame = av.AudioFrame.from_ndarray(arr, format="s16", layout="mono")
            frame.sample_rate = self._src_rate
            yield from _feed(frame)
        yield from _feed(None)  # flush the resampler's internal buffer


def resample_synth(synth: Any, output_rate: int) -> Any:
    """*synth* itself when its rate already matches, else a :class:`ResampledSynth` around it."""
    if int(output_rate) == int(synth.sample_rate):
        return synth
    return ResampledSynth(synth, output_rate)


def split_text_for_tts_stream(text: str, cap: int) -> list:
    """Split *text* into provider-cap-sized pieces on sentence boundaries (whitespace reflowed; no
    fence semantics — deliberately NOT the fence-aware splitter)."""
    boundary = speech.sentence_boundary_re()
    cap = cap if cap and cap > 0 else 4000
    pieces, buf = [], ""
    for sentence in filter(str.strip, boundary.split(text)):
        while len(sentence) > cap:
            pieces.append(sentence[:cap])
            sentence = sentence[cap:]
        if buf and len(buf) + len(sentence) + 1 > cap:
            pieces.append(buf)
            buf = sentence
        else:
            buf = f"{buf} {sentence}" if buf else sentence
    if buf:
        pieces.append(buf)
    return pieces
