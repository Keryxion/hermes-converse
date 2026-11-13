"""Named-device routing records and the public Hermes session resource client.

Only device identities and session references live here; Hermes owns all messages,
retention, compression and title uniqueness. API keys are never persisted.
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3
import threading
import unicodedata
import uuid
from urllib.parse import quote

import aiohttp


class NamedError(Exception):
    def __init__(self, error: str = 'conversation_unavailable', code: int = 4404):
        super().__init__(error)
        self.error, self.code = error, code


def parse_names(frame: dict) -> tuple[str, str] | None:
    """Read the ordinary name fields without a conversation override."""
    if 'conversation' in frame:
        raise NamedError('bad_start', 4400)
    if 'assistant_name' not in frame and 'device_name' not in frame:
        return None
    if any(key in frame for key in ('conversation_id', 'profile', 'name')):
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
    session_id: str | None

    @property
    def title(self) -> str:
        return f'{self.assistant_name}: {self.device_name}'

    @property
    def candidate_id(self) -> str:
        # Survives an accepted create whose HTTP response was lost or the gateway restarted.
        return 'voice_' + uuid.uuid5(uuid.UUID(self.recipient_id), self.assistant_name).hex


class DeviceStore:
    """Profile-scoped routing metadata; DB calls run on worker threads."""
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
                    title TEXT NOT NULL UNIQUE, session_id TEXT UNIQUE,
                    PRIMARY KEY(recipient_id, assistant_name));
            ''')
        return self._db

    def reserve(self, assistant_name: str, device_name: str) -> DeviceConversation:
        with self._lock:
            db = self._connection()
            with db:
                db.execute('INSERT OR IGNORE INTO devices VALUES (?, ?)', (device_name, str(uuid.uuid4())))
                recipient = db.execute('SELECT recipient_id FROM devices WHERE device_name=?', (device_name,)).fetchone()[0]
                db.execute('INSERT OR IGNORE INTO conversations VALUES (?, ?, ?, NULL)',
                           (recipient, assistant_name, f'{assistant_name}: {device_name}'))
                row = db.execute('SELECT session_id FROM conversations WHERE recipient_id=? AND assistant_name=?',
                                 (recipient, assistant_name)).fetchone()
                if row is None:
                    raise NamedError()
                sid = row[0]
            return DeviceConversation(recipient, device_name, assistant_name, sid)

    def forget(self, device: DeviceConversation) -> None:
        """Drop the saved session reference; the gateway conversation owns the history now."""
        with self._lock:
            db = self._connection()
            with db:
                db.execute('UPDATE conversations SET session_id=NULL WHERE recipient_id=? AND assistant_name=?',
                           (device.recipient_id, device.assistant_name))

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None


class NamedSessions:
    """Hermes's public session resource, read only to continue a conversation from before
    conversations were gateway sessions."""
    def __init__(self, base: str, key: str, http: aiohttp.ClientSession):
        self.base, self.http = base, http
        self.headers = {'Authorization': f'Bearer {key}'}

    async def request(self, method, path, **kwargs):
        try:
            async with self.http.request(method, self.base + path, headers=self.headers,
                    allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10), **kwargs) as response:
                try:
                    body = await response.json()
                except (ValueError, aiohttp.ContentTypeError):
                    raise NamedError() from None
                if not isinstance(body, dict):
                    raise NamedError()
                return response.status, body
        except (aiohttp.ClientError, TimeoutError):
            raise NamedError() from None

    async def live_tip(self, session_id: str) -> str | None:
        """The live session continuing *session_id* (compression rotates it); None when it is gone."""
        status, body = await self.request('GET', f'/api/sessions/{quote(session_id, safe="")}/messages',
                                         params={'limit': '1', 'order': 'latest'})
        if status == 404:
            return None
        live = body.get('session_id')
        if status != 200 or not isinstance(live, str) or not live:
            raise NamedError()
        return live
