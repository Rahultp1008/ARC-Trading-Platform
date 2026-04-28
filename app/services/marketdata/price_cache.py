"""
price_cache.py — Redis-backed LTP + Quote + OHLC cache.
Falls back to in-memory dict if Redis is unavailable.

Redis key patterns (per spec 5.5):
  ltp:{symbol}            → last traded price as JSON
  quote:{symbol}          → full quote (bid/ask/volume) as JSON
  ohlc:{symbol}:{interval}→ OHLC candle as JSON
  market_status:{exchange} → open/closed/pre_open
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Try Redis, fall back to in-memory
_redis_client = None
_memory_store: dict[str, str] = {}
_USE_REDIS = False

try:
    import redis
    _redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True, socket_connect_timeout=2)
    _redis_client.ping()
    _USE_REDIS = True
    logger.info("Redis connected — using Redis for price cache")
except Exception:
    logger.info("Redis not available — using in-memory price cache")


def _set(key: str, value: str, ttl: int = 300):
    if _USE_REDIS:
        _redis_client.setex(key, ttl, value)
    else:
        _memory_store[key] = value


def _get(key: str) -> Optional[str]:
    if _USE_REDIS:
        return _redis_client.get(key)
    return _memory_store.get(key)


def _delete(key: str):
    if _USE_REDIS:
        _redis_client.delete(key)
    else:
        _memory_store.pop(key, None)


def _keys(pattern: str) -> list[str]:
    if _USE_REDIS:
        return _redis_client.keys(pattern)
    import fnmatch
    return [k for k in _memory_store.keys() if fnmatch.fnmatch(k, pattern)]


# ── LTP ───────────────────────────────────────────────────────────────────

def set_ltp(symbol: str, price: float, volume: float = 0, timestamp: str | None = None):
    data = {
        "symbol": symbol,
        "ltp": price,
        "volume": volume,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "source": "BINANCE" if "CRYPTO:" in symbol else "KITE",
    }
    _set(f"ltp:{symbol}", json.dumps(data), ttl=60)


def get_ltp(symbol: str) -> Optional[dict]:
    raw = _get(f"ltp:{symbol}")
    return json.loads(raw) if raw else None


def get_all_ltps() -> list[dict]:
    keys = _keys("ltp:*")
    result = []
    for k in keys:
        raw = _get(k)
        if raw:
            result.append(json.loads(raw))
    return result


# ── Full Quote ────────────────────────────────────────────────────────────

def set_quote(symbol: str, data: dict):
    data["timestamp"] = data.get("timestamp", datetime.now(timezone.utc).isoformat())
    _set(f"quote:{symbol}", json.dumps(data), ttl=60)


def get_quote(symbol: str) -> Optional[dict]:
    raw = _get(f"quote:{symbol}")
    return json.loads(raw) if raw else None


# ── OHLC Candles ──────────────────────────────────────────────────────────

def set_ohlc(symbol: str, interval: str, candle: dict):
    _set(f"ohlc:{symbol}:{interval}", json.dumps(candle), ttl=600)


def get_ohlc(symbol: str, interval: str) -> Optional[dict]:
    raw = _get(f"ohlc:{symbol}:{interval}")
    return json.loads(raw) if raw else None


def get_ohlc_series(symbol: str, interval: str) -> list[dict]:
    raw = _get(f"ohlc_series:{symbol}:{interval}")
    return json.loads(raw) if raw else []


def set_ohlc_series(symbol: str, interval: str, candles: list[dict]):
    _set(f"ohlc_series:{symbol}:{interval}", json.dumps(candles), ttl=600)


# ── Market Status ─────────────────────────────────────────────────────────

def set_market_status(exchange: str, status: str):
    data = {"exchange": exchange, "status": status, "updated_at": datetime.now(timezone.utc).isoformat()}
    _set(f"market_status:{exchange}", json.dumps(data), ttl=300)


def get_market_status(exchange: str) -> Optional[dict]:
    raw = _get(f"market_status:{exchange}")
    return json.loads(raw) if raw else None


def get_all_market_statuses() -> list[dict]:
    keys = _keys("market_status:*")
    result = []
    for k in keys:
        raw = _get(k)
        if raw:
            result.append(json.loads(raw))
    return result
