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


def normalize_name(value: str) -> str:
    return unicodedata.normalize('NFC', ' '.join(value.split()))


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
        names.append(normalize_name(value))
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
        return self._db

    def reserve(self, assistant_name: str, device_name: str) -> DeviceConversation:
        with self._lock:
            db = self._connection()
            with db:
                # Named columns: a branch deployed to the same host may have added columns since.
                db.execute('INSERT OR IGNORE INTO devices (device_name, recipient_id) VALUES (?, ?)',
                           (device_name, str(uuid.uuid4())))
                recipient = db.execute('SELECT recipient_id FROM devices WHERE device_name=?', (device_name,)).fetchone()[0]
                db.execute('INSERT OR IGNORE INTO conversations (recipient_id, assistant_name, title) '
                           'VALUES (?, ?, ?)', (recipient, assistant_name, f'{assistant_name}: {device_name}'))
                # A title already taken by another name pair leaves no row: the pair is ambiguous.
                if db.execute('SELECT 1 FROM conversations WHERE recipient_id=? AND assistant_name=?',
                              (recipient, assistant_name)).fetchone() is None:
                    raise NamedError()
            return DeviceConversation(recipient, device_name, assistant_name)

    def names(self) -> list[str]:
        """Every device that has ever connected."""
        with self._lock:
            return [row[0] for row in self._connection().execute('SELECT device_name FROM devices')]

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
