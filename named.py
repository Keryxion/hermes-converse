"""Named-device identity records.

Only device identities live here; Hermes owns every conversation and its messages. API keys are
never persisted.
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3
import threading
import unicodedata
import uuid

class NamedError(Exception):
    def __init__(self, error: str = 'conversation_unavailable', code: int = 4404):
        super().__init__(error)
        self.error, self.code = error, code


def parse_names(frame: dict) -> tuple[str, str]:
    """The ``start`` frame's required names. Both are mandatory and no other identity or
    conversation field is accepted."""
    if any(key in frame for key in ('conversation', 'conversation_id', 'profile', 'name')):
        raise NamedError('bad_start', 4400)
    names = []
    for key in ('assistant_name', 'device_name'):
        value = frame.get(key)
        if (not isinstance(value, str) or not value.strip() or len(value) > 98
                or any(unicodedata.category(c).startswith('C') for c in value)):
            raise NamedError('bad_start', 4400)
        names.append(unicodedata.normalize('NFC', ' '.join(value.split())))
    if len(': '.join(names)) > 100:
        raise NamedError('bad_start', 4400)
    return names[0], names[1]


@dataclass(frozen=True)
class DeviceConversation:
    recipient_id: str
    device_name: str
    assistant_name: str

    @property
    def title(self) -> str:
        return f'{self.assistant_name}: {self.device_name}'

    @property
    def candidate_id(self) -> str:
        # Stable per assistant and device; the gateway chat id is derived from it.
        return 'voice_' + uuid.uuid5(uuid.UUID(self.recipient_id), self.assistant_name).hex


class DeviceStore:
    """Profile-scoped device identities; DB calls run on worker threads."""
    def __init__(self, path=None):
        self._path = path
        self._db = None
        self._lock = threading.RLock()
        self._do_not_disturb: dict[str, bool] = {}  # recipient_id -> the stored setting, once read

    def _connection(self):
        if self._db is None:
            if self._path is None:
                from plugins.plugin_storage import plugin_db
                self._db = plugin_db('hermes-converse', 'devices.db')
            else:
                self._db = sqlite3.connect(self._path, check_same_thread=False)
            self._db.executescript('''
                CREATE TABLE IF NOT EXISTS devices (
                    device_name TEXT PRIMARY KEY, recipient_id TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS conversations (
                    recipient_id TEXT NOT NULL, assistant_name TEXT NOT NULL,
                    title TEXT NOT NULL UNIQUE,
                    PRIMARY KEY(recipient_id, assistant_name));
            ''')
            if 'do_not_disturb' not in {row[1] for row in self._db.execute('PRAGMA table_info(devices)')}:
                with self._db:
                    self._db.execute('ALTER TABLE devices ADD COLUMN do_not_disturb INTEGER NOT NULL DEFAULT 0')
        return self._db

    def reserve(self, assistant_name: str, device_name: str) -> DeviceConversation:
        with self._lock:
            db = self._connection()
            with db:
                db.execute('INSERT OR IGNORE INTO devices (device_name, recipient_id) VALUES (?, ?)',
                           (device_name, str(uuid.uuid4())))
                recipient, on = db.execute('SELECT recipient_id, do_not_disturb FROM devices WHERE device_name=?',
                                           (device_name,)).fetchone()
                db.execute('INSERT OR IGNORE INTO conversations (recipient_id, assistant_name, title) '
                           'VALUES (?, ?, ?)', (recipient, assistant_name, f'{assistant_name}: {device_name}'))
                # A title already taken by another name pair leaves no row: the pair is ambiguous.
                if db.execute('SELECT 1 FROM conversations WHERE recipient_id=? AND assistant_name=?',
                              (recipient, assistant_name)).fetchone() is None:
                    raise NamedError()
            self._do_not_disturb[recipient] = bool(on)
            return DeviceConversation(recipient, device_name, assistant_name)

    def recipient_of(self, conversation_id: str) -> str:
        """The device whose conversation has this gateway chat id; "" when there is none."""
        with self._lock:
            for recipient, assistant_name in self._connection().execute(
                    'SELECT recipient_id, assistant_name FROM conversations'):
                if uuid.uuid5(uuid.UUID(recipient), assistant_name).hex == conversation_id:
                    return recipient
        return ''

    def do_not_disturb(self, recipient_id: str) -> bool:
        """The device's setting as last read or written; no database call, so the event loop may ask."""
        return self._do_not_disturb.get(recipient_id, False)

    def set_do_not_disturb(self, recipient_id: str, on: bool) -> bool:
        """Store the device's setting. True when it changed."""
        with self._lock:
            db = self._connection()
            with db:
                changed = db.execute('UPDATE devices SET do_not_disturb=? WHERE recipient_id=? AND do_not_disturb<>?',
                                     (int(on), recipient_id, int(on))).rowcount > 0
            self._do_not_disturb[recipient_id] = on
            return changed

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
