"""Local audio diagnostics gated by two explicit debug flags."""
import asyncio
from contextlib import contextmanager, suppress
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import stat
import struct
import threading
import time
import uuid
import wave

log = logging.getLogger(__name__)
ROOT = Path.home() / '.hermes/logs/converse-recordings'
MAX_RECORDINGS = 20
MAX_AGE_SECONDS = 24 * 60 * 60
MAX_EVENTS = 2000
MAX_EVENT_BYTES = 2048
MAX_PCM_BYTES = 48000 * 2 * 60
QUEUE_SIZE = 64


def _enabled():
    return (os.environ.get('HERMES_VOICE_DEBUG') == '1'
            and os.environ.get('HERMES_CONVERSE_SERVER_RECORDING') == '1')


@contextmanager
def _locked_directory(*, create=False):
    if create:
        ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise PermissionError('diagnostic directory must be owned by the service user')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.fchmod(fd, 0o700)
        yield fd
    finally:
        os.close(fd)


def _prune(directory):
    """Count retained stems, including legacy captures and sidecars without audio."""
    groups = {}
    cutoff = time.time() - MAX_AGE_SECONDS
    for name in os.listdir(directory):
        stem, _, suffix = name.rpartition('.')
        if ((stem.startswith('input-') and suffix in ('wav', 'jsonl'))
                or (stem.startswith('capture-') and suffix in ('wav', 'json'))):
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            groups.setdefault(stem, {})[suffix] = (name, info)
    for stem, files in tuple(groups.items()):
        if any(info.st_mtime > cutoff for _, info in files.values()):
            continue
        audio = files.get('wav')
        fd = None
        try:
            if audio is not None and stat.S_ISREG(audio[1].st_mode):
                fd = os.open(audio[0], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue  # a recorder still owns this pair
            for name, info in files.values():
                if not stat.S_ISDIR(info.st_mode):
                    os.unlink(name, dir_fd=directory)
            if not any(stat.S_ISDIR(info.st_mode) for _, info in files.values()):
                del groups[stem]
        finally:
            if fd is not None:
                os.close(fd)
    return len(groups)


def _cleanup():
    try:
        with _locked_directory() as directory:
            _prune(directory)
    except (FileNotFoundError, BlockingIOError):
        pass
    except OSError:
        log.warning('diagnostic retention cleanup failed', exc_info=True)


async def maintenance(app):
    async def purge():
        while True:
            await asyncio.to_thread(_cleanup)
            await asyncio.sleep(3600)

    task = asyncio.create_task(purge())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _open_private(directory, name, mode):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory)
    try:
        return os.fdopen(fd, mode, buffering=0)
    except Exception:
        os.close(fd)
        raise


class _Files:
    """Only the recording worker accesses open files."""
    def __init__(self, session):
        self.session = session
        self.wav = self.events = self._wav_file = None
        self.bytes = self.event_count = 0
        self._rms_sum = self._rms_count = 0
        self._carry = b''

    def open(self):
        try:
            with _locked_directory(create=True) as directory:
                if _prune(directory) >= MAX_RECORDINGS or not _enabled():
                    return False
                stem = 'input-' + uuid.uuid4().hex
                try:
                    self._wav_file = _open_private(directory, stem + '.wav', 'wb')
                    fcntl.flock(self._wav_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.wav = wave.open(self._wav_file, 'wb')
                    self.wav.setnchannels(1)
                    self.wav.setsampwidth(2)
                    self.wav.setframerate(self.session._input_rate)
                    self.events = _open_private(directory, stem + '.jsonl', 'wb')
                except Exception:
                    self.close()
                    # A partial pair still counts against retention if removal fails.
                    for suffix in ('.wav', '.jsonl'):
                        with suppress(OSError):
                            os.unlink(stem + suffix, dir_fd=directory)
                    raise
        except BlockingIOError:
            return False
        self.event('recording_started', session_id=self.session.session_id,
                   rate=self.session._input_rate, vad=self.session._detector.name)
        return True

    def event(self, kind, **data):
        if self.event_count < MAX_EVENTS and _enabled():
            line = (json.dumps({'event': kind, 'unix_ms': int(time.time() * 1000), **data}) + '\n').encode()
            if len(line) <= MAX_EVENT_BYTES:
                self.events.write(line)
                self.event_count += 1

    def write(self, item):
        if not _enabled():
            return False
        if isinstance(item, dict):
            self.event('server', **item)
            return True
        if not item:  # mute boundary: never join a sample across it
            self._carry = b''
            return True
        maximum = self.session._input_rate * 2 * 60
        chunk = (self._carry + item)[:maximum - self.bytes]
        end = len(chunk) - len(chunk) % 2
        chunk, self._carry = chunk[:end], chunk[end:]
        rate = self.session._input_rate
        # Bound each write and recheck the recording gate between chunks.
        chunk_size = max(2, (rate // 10) * 2)
        for start in range(0, len(chunk), chunk_size):
            if not _enabled():
                return False
            part = chunk[start:start + chunk_size]
            self.wav.writeframesraw(part)
            for index, (sample,) in enumerate(struct.iter_unpack('<h', part)):
                self._rms_sum += sample * sample
                self._rms_count += 1
                if self._rms_count >= rate // 10:
                    offset = (self.bytes // 2 + index + 1 - self._rms_count) * 1000 / rate
                    self.event('rms', offset_ms=round(offset),
                               rms=round(math.sqrt(self._rms_sum / self._rms_count), 2))
                    self._rms_sum = self._rms_count = 0
            self.bytes += len(part)
        return _enabled() and self.bytes < maximum

    def close(self):
        wav, events, wav_file = self.wav, self.events, self._wav_file
        self.wav = self.events = self._wav_file = None
        for handle in (wav, events, wav_file):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    log.warning('diagnostic recording close failed', exc_info=True)


_mute_disarmed = False
_privacy_lock = threading.Lock()
_write_lock = threading.Lock()
_recorders = set()


def disarm():
    global _mute_disarmed
    with _privacy_lock:
        _mute_disarmed = True
        for recorder in tuple(_recorders):
            recorder._disarmed = True
            recorder._loop.call_soon_threadsafe(recorder.close)
    # Wait until any already-started file operation has finished.
    with _write_lock:
        pass


class ServerRecording:
    def __init__(self, session):
        self._queue = None
        self._task = None
        self._queued_bytes = 0
        self._disarmed = False
        self._loop = asyncio.get_running_loop()
        self._session = session
        lock = getattr(session, '_state_lock', None)
        if lock is not None:
            with lock, _privacy_lock:
                self._disarmed = session._diagnostic_disarmed or _mute_disarmed
                if _enabled() and not self._disarmed:
                    self._queue = asyncio.Queue(maxsize=QUEUE_SIZE + 1)
                    self._task = asyncio.create_task(self._run(session, self._queue))
                    session._diagnostic_recordings.add(self)
                    _recorders.add(self)
        else:
            with _privacy_lock:
                if _enabled() and not _mute_disarmed:
                    self._queue = asyncio.Queue(maxsize=QUEUE_SIZE + 1)
                    self._task = asyncio.create_task(self._run(session, self._queue))
                    _recorders.add(self)

    def _write_if_armed(self, operation, *args):
        with _write_lock:
            if self._disarmed:
                return False
            return operation(*args)

    async def _run(self, session, queue):
        files = _Files(session)
        try:
            if await asyncio.to_thread(self._write_if_armed, files.open):
                deadline = asyncio.get_running_loop().time() + MAX_AGE_SECONDS
                while _enabled():
                    async with asyncio.timeout_at(deadline):
                        item = await queue.get()
                    if item is None:
                        break
                    if isinstance(item, bytes):
                        self._queued_bytes -= len(item)
                    if not await asyncio.to_thread(self._write_if_armed, files.write, item):
                        break
        except TimeoutError:
            pass
        except Exception:
            log.warning('diagnostic recording failed', exc_info=True)
        finally:
            self._queue = None
            lock = getattr(session, '_state_lock', None)
            if lock is not None:
                with lock:
                    session._diagnostic_recordings.discard(self)
            with _privacy_lock:
                _recorders.discard(self)
            await asyncio.to_thread(files.close)

    def _record(self, item):
        queue = self._queue
        if queue is None or self._disarmed:
            return
        if not _enabled() or queue.qsize() >= QUEUE_SIZE:
            self.close()
            return
        if isinstance(item, bytes):
            if self._queued_bytes + len(item) > MAX_PCM_BYTES:
                self.close()
                return
            self._queued_bytes += len(item)
        queue.put_nowait(item)

    def audio(self, data):
        self._record(data[:MAX_PCM_BYTES])

    def frame(self, frame):
        if self._queue is None:
            return
        fields = {}
        for key in ('type', 'turn_id', 'next_action', 'quiet_seconds'):
            value = frame.get(key)
            if (isinstance(value, str) and len(value) <= 128) or isinstance(value, (int, float)):
                fields[key] = value
        if isinstance(frame.get('text'), str):
            fields['text_chars'] = len(frame['text'])
        self._record(fields)

    def disarm(self):
        disarm()

    def close(self):
        queue, self._queue = self._queue, None
        if queue is not None:
            queue.put_nowait(None)

    async def wait_closed(self):
        if self._task is not None:
            await asyncio.shield(self._task)
