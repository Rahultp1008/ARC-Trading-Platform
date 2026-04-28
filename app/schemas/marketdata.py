# app/schemas/marketdata.py
# ─────────────────────────────────────────────────────────────────────────
# Pydantic v2 schemas for all Market Data types.
# Used by REST endpoints, WebSocket messages, and the normalizer.
# ─────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Enums ──────────────────────────────────────────────────────────────────

class PriceSource(str, enum.Enum):
    KITE    = "kite"
    BINANCE = "binance"
    MANUAL  = "manual"


class MarketState(str, enum.Enum):
    PRE_OPEN   = "pre_open"
    OPEN       = "open"
    CLOSED     = "closed"
    POST_CLOSE = "post_close"


class OHLCInterval(str, enum.Enum):
    M1  = "1m"
    M5  = "5m"
    M15 = "15m"
    H1  = "1h"
    D1  = "1d"


# ── NormalizedQuote ────────────────────────────────────────────────────────

class NormalizedQuote(BaseModel):
    """
    Unified tick format for all market data.
    Returned by GET /quotes/{symbol} and pushed via WebSocket.
    """
    symbol         : str
    exchange       : str
    ltp            : float
    open           : Optional[float] = None
    high           : Optional[float] = None
    low            : Optional[float] = None
    close          : Optional[float] = None
    volume         : Optional[int]   = None
    bid            : Optional[float] = None
    ask            : Optional[float] = None
    bid_qty        : Optional[int]   = None
    ask_qty        : Optional[int]   = None
    change         : Optional[float] = None
    change_pct     : Optional[float] = None
    last_trade_qty : Optional[int]   = None
    total_buy_qty  : Optional[int]   = None
    total_sell_qty : Optional[int]   = None
    oi             : Optional[int]   = Field(None, description="Open interest — F&O only")
    timestamp      : Optional[str]   = None
    received_at    : Optional[str]   = None
    source         : Optional[str]   = None
    is_stale       : bool            = False

    model_config = ConfigDict(from_attributes=True)


# ── OHLC ───────────────────────────────────────────────────────────────────

class OHLCBar(BaseModel):
    symbol   : str
    interval : str
    open     : float
    high     : float
    low      : float
    close    : float
    volume   : int   = 0
    timestamp: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ── Market Status ──────────────────────────────────────────────────────────

class MarketStatusSchema(BaseModel):
    exchange  : str
    status    : str
    updated_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ── REST Response helpers ──────────────────────────────────────────────────

class LTPResponse(BaseModel):
    symbol: str
    ltp   : float
    source: Optional[str] = None
    timestamp: Optional[str] = None


class QuoteResponse(BaseModel):
    quote: NormalizedQuote
    market_status: Optional[MarketStatusSchema] = None


class MultiQuoteResponse(BaseModel):
    quotes: list[NormalizedQuote]
    total : int


class OHLCResponse(BaseModel):
    symbol  : str
    interval: str
    bars    : list[OHLCBar]
    total   : int
    is_stale: bool = False
