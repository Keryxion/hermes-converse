"""Raw 20 ms mono Opus packets, using the system libopus (loaded only on opt-in)."""

from __future__ import annotations

import ctypes as ct
import ctypes.util
import functools
import sys
import weakref

RATES = (8000, 12000, 16000, 24000, 48000)
APPLICATION_VOIP = 2048
SET_BITRATE, SET_COMPLEXITY, SET_DTX, GET_LOOKAHEAD = 4002, 4010, 4016, 4027


class OpusError(ValueError):
    pass


@functools.lru_cache(maxsize=1)
def _library():
    name = ctypes.util.find_library("opus")
    if not name:
        raise OSError("Opus audio requires the system libopus library")
    lib = ct.CDLL(name)
    signatures = {
        "opus_encoder_create": (ct.c_void_p, [ct.c_int32, ct.c_int, ct.c_int, ct.POINTER(ct.c_int)]),
        "opus_decoder_create": (ct.c_void_p, [ct.c_int32, ct.c_int, ct.POINTER(ct.c_int)]),
        "opus_encoder_destroy": (None, [ct.c_void_p]),
        "opus_decoder_destroy": (None, [ct.c_void_p]),
        "opus_decoder_ctl": (ct.c_int, [ct.c_void_p, ct.c_int]),
        "opus_encoder_ctl": (ct.c_int, [ct.c_void_p, ct.c_int]),  # remaining arguments are variadic
        "opus_encode": (ct.c_int32, [ct.c_void_p, ct.c_void_p, ct.c_int, ct.c_void_p, ct.c_int32]),
        "opus_decode": (ct.c_int, [ct.c_void_p, ct.c_void_p, ct.c_int32, ct.c_void_p, ct.c_int, ct.c_int]),
        "opus_packet_get_nb_samples": (ct.c_int, [ct.c_void_p, ct.c_int32, ct.c_int32]),
        "opus_packet_get_nb_channels": (ct.c_int, [ct.c_void_p]),
        "opus_strerror": (ct.c_char_p, [ct.c_int]),
    }
    for name, (result, args) in signatures.items():
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = result, args
    return lib


def _check(lib, result):
    if result < 0:
        raise OpusError(lib.opus_strerror(result).decode())
    return result


def _little_endian(data: bytes) -> bytes:
    if sys.byteorder == "little":
        return data
    import array
    samples = array.array("h", data)
    samples.byteswap()
    return samples.tobytes()


class Encoder:
    def __init__(self, rate: int):
        if rate not in RATES:
            raise OpusError("invalid Opus sample rate")
        self.rate, self.samples = rate, rate // 50
        self.frame_bytes = self.samples * 2
        self.lib = lib = _library()
        error = ct.c_int()
        self.state = lib.opus_encoder_create(rate, 1, APPLICATION_VOIP, ct.byref(error))
        _check(lib, error.value)
        self.close = weakref.finalize(self, lib.opus_encoder_destroy, self.state)
        try:
            for request, value in ((SET_BITRATE, 24000), (SET_COMPLEXITY, 5), (SET_DTX, 0)):
                _check(lib, lib.opus_encoder_ctl(self.state, request, ct.c_int(value)))
            delay = ct.c_int()
            _check(lib, lib.opus_encoder_ctl(self.state, GET_LOOKAHEAD, ct.byref(delay)))
            self.lookahead = delay.value
        except BaseException:
            self.close()
            raise

    def encode(self, pcm: bytes) -> bytes:
        if not self.close.alive or len(pcm) != self.frame_bytes:
            raise OpusError("encode requires one complete 20 ms frame and an open encoder")
        output = ct.create_string_buffer(4000)
        size = _check(self.lib, self.lib.opus_encode(
            self.state, _little_endian(pcm), self.samples, output, len(output)))
        return output.raw[:size]


class Decoder:
    def __init__(self, rate: int):
        if rate not in RATES:
            raise OpusError("invalid Opus sample rate")
        self.rate, self.samples = rate, rate // 50
        self.lib = lib = _library()
        error = ct.c_int()
        self.state = lib.opus_decoder_create(rate, 1, ct.byref(error))
        _check(lib, error.value)
        self.close = weakref.finalize(self, lib.opus_decoder_destroy, self.state)

    def reset(self):
        _check(self.lib, self.lib.opus_decoder_ctl(self.state, 4028))  # OPUS_RESET_STATE

    def decode(self, packet: bytes) -> bytes:
        if not self.close.alive or not packet or len(packet) > 1275:
            raise OpusError("invalid Opus packet")
        lib = self.lib
        if (lib.opus_packet_get_nb_channels(packet) != 1
                or lib.opus_packet_get_nb_samples(packet, len(packet), self.rate) != self.samples):
            raise OpusError("expected one 20 ms mono Opus packet")
        output = (ct.c_int16 * self.samples)()
        size = _check(lib, lib.opus_decode(self.state, packet, len(packet), output, self.samples, 0))
        if size != self.samples:
            raise OpusError("invalid Opus duration")
        return _little_endian(bytes(output))
