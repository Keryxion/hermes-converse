"""Named-device identity records.

Only device identities live here; Hermes owns every conversation and its messages. API keys are
never persisted.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
import threading
import time
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


def _settings(raw) -> dict:
    try:
        value = json.loads(raw or '{}')
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


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
            if 'settings' not in [row[1] for row in self._db.execute('PRAGMA table_info(devices)')]:
                with self._db:
                    self._db.execute("ALTER TABLE devices ADD COLUMN settings TEXT NOT NULL DEFAULT '{}'")
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
                # A device that is connecting is not stale: the hourly removal leaves it alone.
                self._merge(db, device_name, last_attached=time.time())
            return DeviceConversation(recipient, device_name, assistant_name)

    def settings(self) -> dict[str, dict]:
        """Every device that has ever connected, with its settings."""
        with self._lock:
            return {name: _settings(raw) for name, raw in
                    self._connection().execute('SELECT device_name, settings FROM devices')}

    def update_settings(self, device_name: str, **values) -> None:
        """Set these keys of the device's settings and keep every other."""
        with self._lock:
            db = self._connection()
            with db:
                self._merge(db, device_name, **values)

    @staticmethod
    def _merge(db, device_name: str, **values) -> None:
        row = db.execute('SELECT settings FROM devices WHERE device_name=?', (device_name,)).fetchone()
        if row is not None:
            db.execute('UPDATE devices SET settings=? WHERE device_name=?',
                       (json.dumps({**_settings(row[0]), **values}), device_name))

    def remove_stale(self, before: float, attached: set[str]) -> list[str]:
        """Remove every device last attached earlier than *before* and not in *attached*, with its
        conversations; the names removed. A row with no ``last_attached`` is given the current
        time, so it ends a full period later."""
        with self._lock:
            db = self._connection()
            removed = []
            with db:
                for name, recipient, raw in db.execute(
                        'SELECT device_name, recipient_id, settings FROM devices').fetchall():
                    last = _settings(raw).get('last_attached')
                    if not isinstance(last, (int, float)):
                        self._merge(db, name, last_attached=time.time())
                    elif last < before and name not in attached:
                        db.execute('DELETE FROM conversations WHERE recipient_id=?', (recipient,))
                        db.execute('DELETE FROM devices WHERE device_name=?', (name,))
                        removed.append(name)
            return removed

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
