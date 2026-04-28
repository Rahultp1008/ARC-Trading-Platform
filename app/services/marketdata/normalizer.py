# app/services/marketdata/normalizer.py
# ─────────────────────────────────────────────────────────────────────────
# Normalization Logic (Section 5.5.1)
#
# Converts raw tick dicts from Kite Connect and Binance WebSocket into
# the standard internal dict format used by price_cache.
#
# Also implements is_quote_stale() — the staleness check design rule.
# ─────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ── is_quote_stale() — design rule ────────────────────────────────────────

def is_quote_stale(timestamp: datetime, exchange: Optional[str] = None) -> bool:
    """
    Returns True if a price tick is older than the configurable threshold.

    Thresholds (from settings / .env):
      NSE / BSE → QUOTE_STALE_THRESHOLD_EQUITY  (default 5s)
      BINANCE   → QUOTE_STALE_THRESHOLD_CRYPTO  (default 10s)
      Others    → QUOTE_STALE_THRESHOLD_DEFAULT (default 15s)

    Naive datetimes are treated as UTC.
    """
    from app.core.config import settings

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)

    age = (datetime.now(timezone.utc) - timestamp).total_seconds()

    if exchange and exchange.upper() in ("NSE", "BSE"):
        return age > getattr(settings, "QUOTE_STALE_THRESHOLD_EQUITY", 5)
    if exchange and exchange.upper() == "BINANCE":
        return age > getattr(settings, "QUOTE_STALE_THRESHOLD_CRYPTO", 10)
    return age > getattr(settings, "QUOTE_STALE_THRESHOLD_DEFAULT", 15)


# ── Kite tick normalizer ──────────────────────────────────────────────────

def normalize_kite_tick(raw: dict) -> Optional[dict]:
    """
    Convert a Kite Connect streaming tick into a standard quote dict.

    Caller must inject before calling:
      raw["_symbol"]   = "NSE:RELIANCE"
      raw["_exchange"] = "NSE"

    Returns a plain dict compatible with price_cache.set_quote().
    """
    try:
        now      = datetime.now(timezone.utc)
        symbol   = raw.get("_symbol", "")
        exchange = raw.get("_exchange", "NSE")
        if not symbol:
            return None

        ohlc  = raw.get("ohlc", {})
        depth = raw.get("depth", {})
        bids  = depth.get("buy",  [{}])
        asks  = depth.get("sell", [{}])

        raw_ts = raw.get("exchange_timestamp") or raw.get("timestamp")
        if isinstance(raw_ts, datetime):
            ex_ts = raw_ts if raw_ts.tzinfo else raw_ts.replace(tzinfo=timezone.utc)
        elif isinstance(raw_ts, str):
            try:
                ex_ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            except ValueError:
                ex_ts = datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        elif isinstance(raw_ts, (int, float)):
            ex_ts = datetime.fromtimestamp(
                raw_ts / 1000 if raw_ts > 1e12 else raw_ts, tz=timezone.utc
            )
        else:
            ex_ts = now

        ltp        = float(raw.get("last_price", 0))
        prev_close = float(ohlc.get("close", 0)) or None
        change     = round(ltp - prev_close, 4) if prev_close and ltp else None
        change_pct = round((change / prev_close) * 100, 4) if change and prev_close else None

        return {
            "symbol":         symbol,
            "exchange":       exchange,
            "ltp":            ltp,
            "open":           float(ohlc.get("open", 0)) or None,
            "high":           float(ohlc.get("high", 0)) or None,
            "low":            float(ohlc.get("low", 0)) or None,
            "close":          prev_close,
            "volume":         int(raw.get("volume", 0)) or None,
            "bid":            float(bids[0].get("price", 0)) if bids else None,
            "ask":            float(asks[0].get("price", 0)) if asks else None,
            "bid_qty":        int(bids[0].get("quantity", 0)) if bids else None,
            "ask_qty":        int(asks[0].get("quantity", 0)) if asks else None,
            "change":         change,
            "change_pct":     change_pct,
            "last_trade_qty": int(raw.get("last_quantity", 0)) or None,
            "total_buy_qty":  int(raw.get("buy_quantity", 0)) or None,
            "total_sell_qty": int(raw.get("sell_quantity", 0)) or None,
            "oi":             int(raw.get("oi", 0)) or None,
            "timestamp":      ex_ts.isoformat(),
            "received_at":    now.isoformat(),
            "source":         "kite",
            "is_stale":       is_quote_stale(ex_ts, exchange),
        }
    except Exception as exc:
        logger.error("normalize_kite_tick failed: %s", exc)
        return None


# ── Binance tick normalizer ───────────────────────────────────────────────

def normalize_binance_tick(raw: dict) -> Optional[dict]:
    """
    Convert a Binance 24hr ticker WebSocket message into a standard quote dict.
    Stream: <symbol>@ticker
    Fields: s=symbol, c=ltp, o=open, h=high, l=low, v=volume,
            b=bid, a=ask, B=bid_qty, A=ask_qty, p=change, P=change_pct,
            E=event_time_ms, Q=last_qty
    """
    try:
        now      = datetime.now(timezone.utc)
        sym      = raw.get("s", "")
        if not sym:
            return None

        symbol   = f"CRYPTO:{sym}"
        event_ms = raw.get("E")
        ex_ts    = (
            datetime.fromtimestamp(event_ms / 1000, tz=timezone.utc)
            if event_ms else now
        )

        ltp        = float(raw.get("c", 0))
        change     = float(raw.get("p", 0)) if raw.get("p") else None
        change_pct = float(raw.get("P", 0)) if raw.get("P") else None

        return {
            "symbol":         symbol,
            "exchange":       "BINANCE",
            "ltp":            ltp,
            "open":           float(raw.get("o", 0)) or None,
            "high":           float(raw.get("h", 0)) or None,
            "low":            float(raw.get("l", 0)) or None,
            "close":          float(raw.get("c", 0)) or None,
            "volume":         int(float(raw.get("v", 0))) or None,
            "bid":            float(raw.get("b", 0)) or None,
            "ask":            float(raw.get("a", 0)) or None,
            "bid_qty":        int(float(raw.get("B", 0))) if raw.get("B") else None,
            "ask_qty":        int(float(raw.get("A", 0))) if raw.get("A") else None,
            "change":         change,
            "change_pct":     change_pct,
            "last_trade_qty": int(float(raw.get("Q", 0))) if raw.get("Q") else None,
            "total_buy_qty":  None,
            "total_sell_qty": None,
            "oi":             None,
            "timestamp":      ex_ts.isoformat(),
            "received_at":    now.isoformat(),
            "source":         "binance",
            "is_stale":       is_quote_stale(ex_ts, "BINANCE"),
        }
    except Exception as exc:
        logger.error("normalize_binance_tick failed: %s", exc)
        return None
