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
MAX_PCM_BYTES = 48000 * 2 * 60
QUEUE_SIZE = 2048


class _Audio(bytes):
    def __new__(cls, data):
        item = super().__new__(cls, data)
        item.timestamp = int(time.time() * 1000)
        return item


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


def _prune(directory, *, reserve=False):
    """Count retained stems, including legacy captures and sidecars without audio."""
    groups = {}
    cutoff = time.time() - MAX_AGE_SECONDS
    for name in os.listdir(directory):
        stem, _, suffix = name.rpartition('.')
        if ((stem.startswith('input-') and suffix in ('wav', 'jsonl'))
                or (stem.startswith('capture-') and suffix in ('wav', 'json'))):
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stem.endswith('-output'):
                stem, suffix = stem[:-7], 'output'
            groups.setdefault(stem, {})[suffix] = (name, info)
    for stem, files in sorted(groups.items(), key=lambda pair: max(info.st_mtime for _, info in pair[1].values())):
        if (not reserve or len(groups) < MAX_RECORDINGS) and any(info.st_mtime > cutoff for _, info in files.values()):
            continue
        audio = files.get('wav')
        fd = None
        try:
            if audio is not None and stat.S_ISREG(audio[1].st_mode):
                try:
                    fd = os.open(audio[0], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                except FileNotFoundError:
                    pass
                if fd is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue  # a recorder still owns this pair
            for name, info in files.values():
                if not stat.S_ISDIR(info.st_mode):
                    with suppress(FileNotFoundError):
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
        self.output = self._output_file = None
        self.output_bytes = 0
        self.bytes = self.event_count = 0
        self._rms_sum = self._rms_count = 0
        self._carry = b''
        self._stem = None
        self._directory_identity = None

    def open(self):
        try:
            with _locked_directory(create=True) as directory:
                if _prune(directory, reserve=True) >= MAX_RECORDINGS or not _enabled():
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
                    self._output_file = _open_private(directory, stem + '-output.wav', 'wb')
                    self.output = wave.open(self._output_file, 'wb')
                    self.output.setnchannels(1)
                    self.output.setsampwidth(2)
                    self.output.setframerate(getattr(self.session, "_recording_output_rate", 24000))
                    self._stem = stem
                    info = os.fstat(directory)
                    self._directory_identity = (info.st_dev, info.st_ino)
                except Exception:
                    self.close()
                    # A partial pair still counts against retention if removal fails.
                    for suffix in ('.wav', '.jsonl', '-output.wav'):
                        with suppress(OSError):
                            os.unlink(stem + suffix, dir_fd=directory)
                    raise
        except BlockingIOError:
            return False
        self.event('recording_started', session_id=self.session.session_id,
                   rate=self.session._input_rate, output_rate=getattr(self.session, "_recording_output_rate", 24000),
                   device_name=getattr(self.session, "device_name", None),
                   vad=self.session._detector.name)
        return True

    def event(self, kind, **data):
        if _enabled():
            line = (json.dumps({'event': kind, 'unix_ms': int(time.time() * 1000), **data}) + '\n').encode()
            self.events.write(line)
            self.event_count += 1

    def _current(self):
        """Open handles must still belong to all three named files in the current directory."""
        if self._stem is None:
            return False
        try:
            info = os.stat(ROOT, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != self._directory_identity:
                return False
            for suffix, handle in (('.wav', self._wav_file), ('.jsonl', self.events),
                                   ('-output.wav', self._output_file)):
                named = os.stat(ROOT / (self._stem + suffix), follow_symlinks=False)
                opened = os.fstat(handle.fileno())
                if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                    return False
            return True
        except FileNotFoundError:
            return False

    def write(self, item):
        if not _enabled():
            return False
        if not self._current():
            self.close()
            self.__init__(self.session)
            try:
                if not self.open():
                    return False
            except FileNotFoundError:
                # Another deletion raced reopening; retry on the next queued event.
                self.close()
                return True
        if isinstance(item, dict):
            self.event(item.pop('event'), **item)
            return True
        if isinstance(item, tuple):
            data, timestamp = item
            self.event('audio', direction='output', received_unix_ms=timestamp,
                       offset_samples=self.output_bytes // 2, samples=len(data) // 2)
            self.output.writeframes(data)
            self.output_bytes += len(data)
            return True
        if not item:  # mute boundary: never join a sample across it
            self._carry = b''
            return True
        self.event("audio", direction="input", received_unix_ms=getattr(item, "timestamp", None),
                   offset_samples=self.bytes // 2, samples=len(item) // 2)
        chunk = self._carry + item
        end = len(chunk) - len(chunk) % 2
        chunk, self._carry = chunk[:end], chunk[end:]
        rate = self.session._input_rate
        # Bound each write and recheck the recording gate between chunks.
        chunk_size = max(2, (rate // 10) * 2)
        for start in range(0, len(chunk), chunk_size):
            if not _enabled():
                return False
            part = chunk[start:start + chunk_size]
            self.wav.writeframes(part)
            for index, (sample,) in enumerate(struct.iter_unpack('<h', part)):
                self._rms_sum += sample * sample
                self._rms_count += 1
                if self._rms_count >= rate // 10:
                    offset = (self.bytes // 2 + index + 1 - self._rms_count) * 1000 / rate
                    self.event('rms', offset_ms=round(offset),
                               rms=round(math.sqrt(self._rms_sum / self._rms_count), 2))
                    self._rms_sum = self._rms_count = 0
            self.bytes += len(part)
        return _enabled()

    def close(self):
        wav, events, wav_file = self.wav, self.events, self._wav_file
        self.wav = self.events = self._wav_file = None
        self._stem = self._directory_identity = None
        output, output_file = self.output, self._output_file
        self.output = self._output_file = None
        for handle in (wav, events, wav_file, output, output_file):
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
    def __init__(self, session, *, output_rate=24000):
        session._recording_output_rate = output_rate
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
                    self._queue = asyncio.Queue(maxsize=QUEUE_SIZE + 2)
                    self._task = asyncio.create_task(self._run(session, self._queue))
                    session._diagnostic_recordings.add(self)
                    _recorders.add(self)
        else:
            with _privacy_lock:
                if _enabled() and not _mute_disarmed:
                    self._queue = asyncio.Queue(maxsize=QUEUE_SIZE + 2)
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
            try:
                opened = await asyncio.to_thread(self._write_if_armed, files.open)
            except FileNotFoundError:
                opened = True  # deletion raced the first open; retry with the next event
            if opened:
                deadline = asyncio.get_running_loop().time() + MAX_AGE_SECONDS
                while _enabled():
                    async with asyncio.timeout_at(deadline):
                        item = await queue.get()
                    if item is None:
                        break
                    if isinstance(item, bytes):
                        self._queued_bytes -= len(item)
                    elif isinstance(item, tuple):
                        self._queued_bytes -= len(item[0])
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
        if not _enabled():
            self.close()
            return
        if queue.qsize() >= QUEUE_SIZE:
            self._overflow('queue_full')
            return
        size = len(item) if isinstance(item, bytes) else len(item[0]) if isinstance(item, tuple) else 0
        if size:
            if self._queued_bytes + size > MAX_PCM_BYTES:
                self._overflow('queued_audio_limit')
                return
            self._queued_bytes += size
        queue.put_nowait(item)

    def _overflow(self, reason):
        log.warning('diagnostic recording stopped: session=%s reason=%s', self._session.session_id, reason)
        if self._queue is not None:
            self._queue.put_nowait({'event': 'recording_stopped', 'reason': reason,
                                    'received_unix_ms': int(time.time() * 1000)})
        self.close()

    def diagnostic(self, frame):
        self.frame(frame, direction='diagnostic')

    def audio(self, data):
        self._record(_Audio(data[:MAX_PCM_BYTES]))

    def output_audio(self, data):
        self._record((data, int(time.time() * 1000)))

    def frame(self, frame, *, direction='server'):
        self._record({'event': direction, 'frame': frame,
                      'received_unix_ms': int(time.time() * 1000)})

    def disarm(self):
        disarm()

    def close(self):
        queue, self._queue = self._queue, None
        if queue is not None:
            queue.put_nowait(None)

    async def wait_closed(self):
        if self._task is not None:
            await asyncio.shield(self._task)


def record_for_sessions(session_ids, frame):
    with _privacy_lock:
        recorders = tuple(_recorders)
    for recorder in recorders:
        if recorder._session.session_id in session_ids:
            recorder.diagnostic(frame)
