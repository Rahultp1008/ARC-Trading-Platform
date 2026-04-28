# app/services/marketdata/quote_service.py
# ─────────────────────────────────────────────────────────────────────────
# Quote Service — business logic layer for market data.
#
# Routes call this service. The service calls price_cache.
# This separates concerns: routes stay thin, logic stays here.
#
# Uses price_cache (sync Redis + in-memory fallback) — never crashes.
# ─────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import logging
from typing import Optional

from app.services.marketdata import price_cache
from app.services.marketdata.normalizer import is_quote_stale

logger = logging.getLogger(__name__)


def get_quote(symbol: str) -> Optional[dict]:
    """
    Get latest full quote for a symbol.
    Refreshes is_stale flag before returning.
    """
    quote = price_cache.get_quote(symbol)
    if not quote:
        # Fallback: try LTP only and wrap it
        ltp = price_cache.get_ltp(symbol)
        if ltp:
            return ltp
        return None

    # Refresh staleness
    ts_str = quote.get("timestamp")
    if ts_str:
        try:
            from datetime import datetime, timezone
            ts = datetime.fromisoformat(ts_str)
            quote["is_stale"] = is_quote_stale(ts, quote.get("exchange"))
        except Exception:
            pass

    return quote


def get_ltp(symbol: str) -> Optional[dict]:
    """Get LTP-only response for a symbol."""
    return price_cache.get_ltp(symbol)


def get_all_ltps(segment: Optional[str] = None) -> list[dict]:
    """
    Get all cached LTPs, optionally filtered by segment.
    segment: CASH → NSE: prefix | FNO → NFO: prefix | CRYPTO → CRYPTO: prefix
    """
    ltps = price_cache.get_all_ltps()
    if segment:
        seg = segment.upper()
        prefix_map = {"CASH": "NSE:", "FNO": "NFO:", "CRYPTO": "CRYPTO:"}
        prefix = prefix_map.get(seg, seg + ":")
        ltps = [l for l in ltps if l.get("symbol", "").startswith(prefix)]
    return ltps


def get_ohlc(symbol: str, interval: str = "1m") -> Optional[dict]:
    """Get latest OHLC candle for a symbol."""
    return price_cache.get_ohlc(symbol, interval)


def get_ohlc_series(symbol: str, interval: str = "1m") -> list[dict]:
    """Get last 30 OHLC candles for a symbol."""
    return price_cache.get_ohlc_series(symbol, interval)


def get_market_status(exchange: str) -> Optional[dict]:
    """Get market open/closed status for an exchange."""
    return price_cache.get_market_status(exchange)


def get_all_market_statuses() -> list[dict]:
    """Get market status for all exchanges."""
    return price_cache.get_all_market_statuses()


async def ingest_tick(quote_dict: dict) -> None:
    """
    Ingest a normalized tick dict from a tick adapter.
    Writes to price_cache and fans out to WebSocket clients.
    """
    symbol = quote_dict.get("symbol", "")
    ltp    = quote_dict.get("ltp", 0)
    vol    = quote_dict.get("volume") or 0

    price_cache.set_ltp(symbol, ltp, volume=vol)
    price_cache.set_quote(symbol, quote_dict)

    # WS fanout
    from app.services.marketdata.ws_manager import manager
    await manager.broadcast_tick(symbol, {**quote_dict, "type": "tick"})
