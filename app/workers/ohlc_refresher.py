# app/workers/ohlc_refresher.py
# ─────────────────────────────────────────────────────────────────────────
# OHLC Refresher Background Worker (Section 5.5.5)
#
# Reads all LTP values from price_cache every second and builds/updates
# OHLC candles for each configured interval (1m, 5m, 15m, 1h, 1d).
#
# Uses price_cache (Redis + in-memory fallback) — NEVER crashes.
# Started automatically in app/main.py lifespan.
# ─────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_INTERVAL_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400,
}


def _truncate(dt: datetime, interval: str) -> datetime:
    secs = _INTERVAL_SECONDS.get(interval, 60)
    ts   = (int(dt.timestamp()) // secs) * secs
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class _CandleState:
    def __init__(self):
        self.open : Optional[float]    = None
        self.high : float              = 0.0
        self.low  : float              = float("inf")
        self.close: float              = 0.0
        self.volume: int               = 0
        self.ts   : Optional[datetime] = None

    def update(self, price: float, candle_start: datetime) -> Optional[dict]:
        completed = None
        if self.ts and candle_start != self.ts:
            if self.open is not None:
                completed = self._to_dict()
            self._reset(price, candle_start)
        elif self.ts is None:
            self._reset(price, candle_start)
        else:
            self.high  = max(self.high, price)
            self.low   = min(self.low, price)
            self.close = price
        return completed

    def current(self, symbol: str, interval: str) -> Optional[dict]:
        if self.open is None or self.ts is None:
            return None
        d = self._to_dict()
        d["symbol"]   = symbol
        d["interval"] = interval
        return d

    def _to_dict(self) -> dict:
        return {
            "open": self.open, "high": self.high,
            "low":  self.low,  "close": self.close,
            "volume": self.volume,
            "timestamp": self.ts.isoformat() if self.ts else None,
        }

    def _reset(self, price: float, ts: datetime):
        self.open   = price
        self.high   = price
        self.low    = price
        self.close  = price
        self.volume = random.randint(100, 10000)
        self.ts     = ts


class OHLCRefresher:

    def __init__(self, poll_interval: float = 1.0):
        self._poll    = poll_interval
        self._running = False
        self._task    : Optional[asyncio.Task] = None
        self._candles : dict[tuple, _CandleState] = {}

    async def start(self):
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("OHLCRefresher started (poll=%.1fs)", self._poll)

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("OHLCRefresher stopped.")

    async def _loop(self):
        while self._running:
            try:
                # Run sync price_cache calls in executor to avoid blocking event loop
                await asyncio.get_event_loop().run_in_executor(None, self._tick_sync)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("OHLCRefresher._tick error: %s", exc)
            await asyncio.sleep(self._poll)

    def _tick_sync(self):
        from app.services.marketdata.price_cache import get_all_ltps, set_ohlc
        from app.core.config import settings

        now       = datetime.now(timezone.utc)
        ltps      = get_all_ltps()
        intervals = getattr(settings, "OHLC_INTERVALS", ["1m", "5m", "15m", "1h", "1d"])

        for item in ltps:
            symbol = item.get("symbol", "")
            price  = float(item.get("ltp") or item.get("price") or 0)
            if not symbol or not price:
                continue

            for interval in intervals:
                key   = (symbol, interval)
                state = self._candles.setdefault(key, _CandleState())
                completed = state.update(price, _truncate(now, interval))

                if completed:
                    completed["symbol"]   = symbol
                    completed["interval"] = interval
                    set_ohlc(symbol, interval, completed)

                current = state.current(symbol, interval)
                if current:
                    set_ohlc(symbol, interval, current)


# Singleton
ohlc_refresher = OHLCRefresher()
