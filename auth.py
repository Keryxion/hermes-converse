"""Authentication for the converse socket: the client's own ``API_SERVER_KEY``, verified by asking
the API server itself, plus an optional scoped ``voice_token`` that authorizes only this voice
route.

The plugin never reads the ``API_SERVER_KEY`` (catalog rule 11) and never compares it: it presents
the client's candidate to ``GET /v1/capabilities`` over loopback with ``Authorization: Bearer`` and
admits the socket only on 200. The same key then drives the connection's ``/v1/runs`` calls, so an
existing client keeps the one secret it already has, a rotated key fails on the next connect, and
the server's own per-profile key semantics apply unchanged.

A scoped ``voice_token`` is a voice-only credential: instead of sending the full ``API_SERVER_KEY``
over a plain ``ws://`` socket, a client presents ``start.voice_token`` (or the
``hermes-voice-token.<token>`` subprotocol) and the API server accepts it as Bearer for
``/v1/capabilities`` and ``/v1/runs``. A sniffed voice token gives away only voice turns, not the
full API. When ``settings.voice_token`` is set the plugin additionally accepts a voice token by
local constant-time compare without a server probe, so a deployment can scope a voice token even
when the API server itself does not know it.
"""

from __future__ import annotations

import hmac
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = aiohttp.ClientTimeout(total=5)
KEY_PROTOCOL_PREFIX = "hermes-key."
VOICE_TOKEN_PROTOCOL_PREFIX = "hermes-voice-token."


def offered_key_protocol(protocol_header: str) -> Optional[str]:
    """The single ``hermes-key.<KEY>`` value offered in ``Sec-WebSocket-Protocol``, or ``None`` when
    no key subprotocol was offered at all. An empty string means one was offered but malformed or
    ambiguous (two of them) — reject."""
    offered = [value.strip() for value in (protocol_header or "").split(",") if value.strip()]
    key_protocols = [v for v in offered if v.startswith(KEY_PROTOCOL_PREFIX)]
    if not key_protocols:
        return None
    if len(key_protocols) != 1:
        return ""
    return key_protocols[0][len(KEY_PROTOCOL_PREFIX):]


def offered_voice_token_protocol(protocol_header: str) -> Optional[str]:
    """The single ``hermes-voice-token.<TOKEN>`` value offered in ``Sec-WebSocket-Protocol``, or
    ``None`` when no voice-token subprotocol was offered at all. An empty string means one was
    offered but malformed or ambiguous (two of them) — reject."""
    offered = [value.strip() for value in (protocol_header or "").split(",") if value.strip()]
    voice_protocols = [v for v in offered if v.startswith(VOICE_TOKEN_PROTOCOL_PREFIX)]
    if not voice_protocols:
        return None
    if len(voice_protocols) != 1:
        return ""
    return voice_protocols[0][len(VOICE_TOKEN_PROTOCOL_PREFIX):]


async def verify_api_key(base_url: str, candidate: str, *, http: aiohttp.ClientSession) -> bool:
    """True only when the API server accepts *candidate* as a Bearer key. Fails closed: an empty
    candidate, a timeout, a redirect or any non-200 answer refuses the socket. Works for the full
    ``API_SERVER_KEY`` or a scoped ``voice_token`` the server is configured to accept."""
    if not candidate:
        return False
    try:
        async with http.get(f"{base_url.rstrip('/')}/v1/capabilities",
                            headers={"Authorization": f"Bearer {candidate}"},
                            allow_redirects=False, timeout=PROBE_TIMEOUT) as resp:
            await resp.read()
            if resp.status == 200:
                return True
            if resp.status == 401:
                logger.warning("converse: client presented an invalid credential")
            else:
                logger.warning("converse: credential probe answered %s; refusing the socket", resp.status)
            return False
    except Exception as exc:
        logger.warning("converse: credential probe failed (%s); refusing the socket", type(exc).__name__)
        return False


def verify_voice_token(candidate: str, expected: str) -> bool:
    """True when *candidate* matches the configured *expected* voice token, compared in constant
    time. An empty candidate, or no configured voice token, never authenticates."""
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate, expected)
