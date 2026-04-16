"""
app/schemas/instrument.py
Module 4 — Instrument Master | ARC Trading Platform
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.instruments import InstrumentType, OptionType, PriceSource, Segment


# ── Shared base ───────────────────────────────────────────────────────────────

class _Base(BaseModel):
    symbol:            str            = Field(..., max_length=64)
    exchange:          str            = Field(..., max_length=32)
    display_name:      str            = Field(..., max_length=128)
    instrument_type:   InstrumentType
    segment:           Segment
    tick_size:         Decimal        = Field(..., gt=0)
    lot_size:          Decimal        = Field(..., gt=0)
    is_active:         bool           = True
    trading_allowed:   bool           = True
    price_source:      PriceSource
    expiry:            Optional[date]        = None
    strike:            Optional[Decimal]     = None
    option_type:       Optional[OptionType]  = None
    underlying_symbol: Optional[str]         = Field(None, max_length=64)
    isin:              Optional[str]         = Field(None, max_length=16)
    series:            Optional[str]         = Field(None, max_length=8)
    is_index:          bool                  = False

    model_config = {"from_attributes": True}


# ── Full read (GET /instruments/{id}) ─────────────────────────────────────────

class InstrumentRead(_Base):
    """Complete record returned by GET /api/v1/instruments/{id}."""
    id:              uuid.UUID
    external_symbol: Optional[str]            = None
    external_token:  Optional[str]            = None
    metadata_json:   Optional[dict[str, Any]] = None
    created_at:      datetime
    updated_at:      datetime
    last_synced_at:  Optional[datetime]       = None

    @field_validator("tick_size", "lot_size", "strike", mode="before")
    @classmethod
    def _coerce(cls, v: Any) -> Optional[Decimal]:
        return Decimal(str(v)) if v is not None else None


# ── Lightweight summary (search result rows / market list) ────────────────────

class InstrumentSummary(BaseModel):
    """
    Condensed view for market lists, watchlists, and order-ticket search.

    UI tab routing — frontend reads `segment`:
      CASH   →  Equities / ETFs / Indices tab
      FNO    →  Futures & Options tab
      CRYPTO →  Crypto Paper Trading tab
    """
    id:              uuid.UUID
    symbol:          str
    display_name:    str
    exchange:        str
    instrument_type: InstrumentType
    segment:         Segment           # ← UI tab key
    tick_size:       Decimal
    lot_size:        Decimal
    expiry:          Optional[date]        = None
    option_type:     Optional[OptionType]  = None
    strike:          Optional[Decimal]     = None
    is_active:       bool
    trading_allowed: bool
    is_index:        bool
    price_source:    PriceSource

    model_config = {"from_attributes": True}


# ── Search query params (GET /instruments) ────────────────────────────────────

class InstrumentSearch(BaseModel):
    """
    All filters for GET /api/v1/instruments.

    Text search (q):
      Prefix match:  symbol ILIKE 'RELI%'   always applied
      Fuzzy match:   pg_trgm similarity > 0.3  (requires CREATE EXTENSION pg_trgm)
      Falls back to prefix-only when extension is absent.

    UI tab filter (segment):
      CASH / FNO / CRYPTO  →  powers the three market list tabs.
      Omit to search across all segments.

    F&O option-chain drill-down:
      underlying_symbol + option_type + expiry_from/to + min/max_strike.
    """

    q:                 Optional[str]          = Field(None, max_length=100,
                           description="Prefix or fuzzy search on symbol / display_name")
    segment:           Optional[Segment]        = Field(None, description="CASH | FNO | CRYPTO")
    instrument_type:   Optional[InstrumentType] = None
    exchange:          Optional[str]            = Field(None, max_length=32)
    underlying_symbol: Optional[str]            = Field(None, max_length=64)
    option_type:       Optional[OptionType]     = None
    expiry_from:       Optional[date]           = None
    expiry_to:         Optional[date]           = None
    min_strike:        Optional[Decimal]        = None
    max_strike:        Optional[Decimal]        = None
    is_active:         Optional[bool]           = Field(True, description="Default: active only")
    trading_allowed:   Optional[bool]           = None
    page:              int                      = Field(1,  ge=1)
    page_size:         int                      = Field(20, ge=1, le=100)

    @model_validator(mode="after")
    def _check_ranges(self) -> "InstrumentSearch":
        if self.expiry_from and self.expiry_to and self.expiry_from > self.expiry_to:
            raise ValueError("expiry_from must be ≤ expiry_to")
        if (self.min_strike is not None and self.max_strike is not None
                and self.min_strike > self.max_strike):
            raise ValueError("min_strike must be ≤ max_strike")
        return self

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


# ── Paginated response ────────────────────────────────────────────────────────

class InstrumentSearchResponse(BaseModel):
    items:       list[InstrumentSummary]
    total:       int
    page:        int
    page_size:   int
    total_pages: int


# ── Admin toggle ──────────────────────────────────────────────────────────────

class InstrumentToggleRequest(BaseModel):
    """
    Body for PATCH /api/v1/instruments/{id}/toggle — Super Admin only.

    trading_allowed = False  →  soft block: orders rejected, instrument still visible
    is_active       = False  →  hard block: hidden from all searches (delisted)
    """
    trading_allowed: bool          = Field(...,
        description="False = suspend order placement; instrument stays visible")
    is_active:       Optional[bool] = Field(None,
        description="False = hide from all searches")
    reason:          Optional[str]  = Field(None, max_length=512,
        description="Audit note describing why trading is toggled")


class InstrumentToggleResponse(BaseModel):
    id:              uuid.UUID
    symbol:          str
    trading_allowed: bool
    is_active:       bool
    message:         str


# ── Sync result ───────────────────────────────────────────────────────────────

class InstrumentSyncResult(BaseModel):
    source:            PriceSource
    status:            str          # success | partial | failed
    total_fetched:     int
    total_created:     int
    total_updated:     int
    total_deactivated: int
    errors:            list[str]    = []
    started_at:        datetime
    finished_at:       datetime
    duration_seconds:  float
