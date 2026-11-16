"""Opt-in, bounded local audio diagnostics; never exported to telemetry."""
import json
import logging
import os
from pathlib import Path
import shutil
import time
import uuid
import wave

log = logging.getLogger(__name__)
ROOT = Path.home()/'.hermes/logs/converse-recordings'
# Disabled unless an operator explicitly opts in for a short diagnostic window.
ENABLED = os.environ.get('CONVERSE_DIAGNOSTIC_RECORDING') == '1'
UNTIL = time.monotonic() + 1800 if ENABLED else 0
sockets = captures = 0


def directory():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    ROOT.chmod(0o700)
    return ROOT


def save_capture(path, session_id):
    global captures
    if time.monotonic() > UNTIL or captures >= 20:
        return
    try:
        captures += 1
        stem = 'capture-' + uuid.uuid4().hex[:12]
        target = directory()/(stem+'.wav')
        shutil.copyfile(path, target)
        target.chmod(0o600)
        meta = target.with_suffix('.json')
        meta.write_text(json.dumps({'session_id': session_id, 'unix_ms': int(time.time()*1000)}))
        meta.chmod(0o600)
        log.info('converse diagnostic capture saved: %s session=%s', target.name, session_id)
    except Exception:
        log.warning('converse diagnostic capture failed', exc_info=True)


class ServerRecording:
    def __init__(self, session):
        global sockets
        self.session = session
        self.wav = self.events = None
        self.bytes = 0
        if not ENABLED or time.monotonic() > UNTIL or sockets >= 20:
            return
        try:
            sockets += 1
            stem = 'input-' + uuid.uuid4().hex[:12]
            self.wav_path = directory()/(stem+'.wav')
            self.wav = wave.open(str(self.wav_path), 'wb')
            self.wav.setnchannels(1)
            self.wav.setsampwidth(2)
            self.wav.setframerate(session._input_rate)
            self.wav_path.chmod(0o600)
            event_path = self.wav_path.with_suffix('.jsonl')
            self.events = event_path.open('w', buffering=1)
            event_path.chmod(0o600)
            self.event('recording_started', session_id=session.session_id, rate=session._input_rate,
                       vad=session._detector.name)
            log.info('converse server-side recording: %s session=%s', self.wav_path.name, session.session_id)
        except Exception:
            log.warning('converse diagnostic recording unavailable', exc_info=True)
            self.close()

    def event(self, kind, **data):
        if self.events is not None and time.monotonic() <= UNTIL:
            try:
                self.events.write(json.dumps({'event': kind, 'unix_ms': int(time.time()*1000), **data})+'\n')
            except Exception:
                pass

    def audio(self, data):
        if self.wav is None:
            return
        try:
            maximum = self.session._input_rate * 2 * 60
            chunk = data[:max(0, maximum-self.bytes)]
            self.wav.writeframes(chunk)
            self.bytes += len(chunk)
            if self.bytes >= maximum:
                self.event('audio_limit')
                self.wav.close()
                self.wav = None
        except Exception:
            log.warning('converse diagnostic input failed', exc_info=True)
            self.close()

    def frame(self, frame):
        fields = {k: frame[k] for k in ('type', 'turn_id', 'next_action', 'quiet_seconds') if k in frame}
        if 'text' in frame:
            fields['text_chars'] = len(frame['text'])
        self.event('server', **fields)

    def close(self):
        self.event('recording_closed')
        if self.wav is not None:
            self.wav.close()
            self.wav = None
        if self.events is not None:
            self.events.close()
            self.events = None
