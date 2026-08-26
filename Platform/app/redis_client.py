"""Lazy Redis client with graceful fallback.

The platform remains fully functional without Redis: every helper here returns
``None`` (or a no-op) when ``REDIS_URL`` is unset or unreachable. This keeps the
existing SQLite-backed pipeline worker and test suite working out of the box,
while production enables realtime queue/pub-sub/cache by setting ``REDIS_URL``.
"""

from __future__ import annotations

import os
from typing import Any

_client = None
_client_resolved = False


def redis_url() -> str:
    return os.getenv("REDIS_URL", "").strip()


def get_redis():
    """Return a shared Redis client, or ``None`` when Redis is not configured.

    Resolution happens once; a failed connection does not raise. Callers must
    treat the result as optional.
    """
    global _client, _client_resolved
    if _client_resolved:
        return _client
    _client_resolved = True
    url = redis_url()
    if not url:
        return None
    try:
        import redis as _redis

        _client = _redis.from_url(url, decode_responses=True)
        # Do not block boot on a slow/unreachable broker.
        _client.ping()
    except Exception:
        _client = None
    return _client


def is_available() -> bool:
    return get_redis() is not None




def ping() -> bool:
    """Actively verify the cached Redis connection for readiness probes."""
    client = get_redis()
    if client is None:
        return False
    try:
        return bool(client.ping())
    except Exception:
        return False


def publish(channel: str, payload: Any) -> bool:
    """Publish a JSON payload to a channel. Returns False (silently) on failure."""
    client = get_redis()
    if client is None:
        return False
    try:
        import json

        client.publish(channel, json.dumps(payload, ensure_ascii=False, default=str))
        return True
    except Exception:
        return False


def subscribe(channel: str):
    """Yield decoded messages from a channel until the consumer stops iterating.

    A generator so it can be consumed lazily by an SSE view. When Redis is
    unavailable this yields nothing (callers fall back to polling).
    """
    client = get_redis()
    if client is None:
        return
    try:
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(channel)
        try:
            for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                data = message.get("data")
                if data is None:
                    continue
                import json

                try:
                    yield json.loads(data)
                except (TypeError, ValueError):
                    yield {"raw": data}
        finally:
            pubsub.close()
    except Exception:
        return
