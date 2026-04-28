# app/core/redis.py
# ─────────────────────────────────────────────────────────────────────────
# Optional async Redis connection pool.
#
# GRACEFUL DESIGN: If Redis is not running, init_redis_pool() logs a
# warning and sets _pool = None. get_redis() returns None (not an error).
# All callers must check for None and fall back to price_cache in-memory.
#
# This means the server ALWAYS starts, whether Redis is up or not.
# ─────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_pool: Optional[object] = None
_redis_available: bool = False


async def init_redis_pool() -> bool:
    """
    Try to connect to Redis. Returns True if connected, False if not available.
    Never raises — server always starts even if Redis is down.
    """
    global _pool, _redis_available
    try:
        import redis.asyncio as aioredis
        from app.core.config import settings
        redis_url = getattr(settings, "REDIS_URL", "redis://localhost:6379/0")
        _pool = aioredis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=getattr(settings, "REDIS_MAX_CONNECTIONS", 20),
        )
        await _pool.ping()
        _redis_available = True
        logger.info("Redis async pool connected: %s", redis_url)
        return True
    except Exception as exc:
        _pool = None
        _redis_available = False
        logger.warning("Redis not available (using in-memory fallback): %s", exc)
        return False


async def close_redis_pool() -> None:
    global _pool, _redis_available
    if _pool is not None:
        try:
            await _pool.close()
        except Exception:
            pass
        _pool = None
        _redis_available = False
        logger.info("Redis async pool closed.")


def get_redis():
    """Return async Redis client or None if not available."""
    return _pool


def is_redis_available() -> bool:
    return _redis_available
