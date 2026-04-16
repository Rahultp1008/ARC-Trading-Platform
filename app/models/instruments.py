"""
app/models/instruments.py
Module 4 — Instrument Master | ARC Trading Platform

FIXED: Uses shared Base from app.core.database (not a standalone Base).
This ensures Alembic sees the instruments table and auto-create works.
"""
from __future__ import annotations

import enum
import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Enum,
    Index, Numeric, String, Text, func, Integer,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base   # shared Base — required for Alembic


# ── Enums ──────────────────────────────────────────────────────────────────────

class InstrumentType(str, enum.Enum):
    EQUITY   = "equity"
    ETF      = "etf"
    FUTURES  = "futures"
    OPTIONS  = "options"
    INDEX    = "index"
    CRYPTO   = "crypto"


class Segment(str, enum.Enum):
    CASH   = "cash"
    FNO    = "fno"
    CRYPTO = "crypto"


class OptionType(str, enum.Enum):
    CE = "CE"
    PE = "PE"


class PriceSource(str, enum.Enum):
    KITE    = "kite"
    BINANCE = "binance"
    MANUAL  = "manual"


# ── Instrument table ────────────────────────────────────────────────────────────

class Instrument(Base):
    __tablename__ = "instruments"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )
    symbol           = Column(String(100),  unique=True, nullable=False, index=True)
    external_symbol  = Column(String(100),  nullable=False, index=True)
    external_token   = Column(String(100),  nullable=True)
    exchange         = Column(String(20),   nullable=False, index=True)
    display_name     = Column(String(200),  nullable=False)
    instrument_type  = Column(Enum(InstrumentType), nullable=False, index=True)
    segment          = Column(Enum(Segment),         nullable=False, index=True)
    tick_size        = Column(Numeric(18, 8), nullable=False, default=Decimal("0.05"))
    lot_size         = Column(Numeric(18, 8), nullable=False, default=Decimal("1"))
    expiry           = Column(Date,   nullable=True)
    strike           = Column(Numeric(18, 4), nullable=True)
    option_type      = Column(Enum(OptionType), nullable=True)
    underlying_symbol = Column(String(100),  nullable=True)
    price_source     = Column(Enum(PriceSource), nullable=False, default=PriceSource.KITE)
    is_active        = Column(Boolean, nullable=False, default=True, index=True)
    trading_allowed  = Column(Boolean, nullable=False, default=True)
    is_index         = Column(Boolean, nullable=False, default=False)
    isin             = Column(String(20),  nullable=True)
    series           = Column(String(10),  nullable=True)
    metadata_json    = Column(JSONB,       nullable=True)
    created_at       = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at       = Column(DateTime(timezone=True), server_default=func.now(),
                              onupdate=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_instruments_type_segment",  "instrument_type", "segment"),
        Index("ix_instruments_exchange_active", "exchange", "is_active"),
    )

    def __repr__(self) -> str:
        return f"<Instrument {self.symbol} ({self.instrument_type.value})>"


# ── Sync log table ──────────────────────────────────────────────────────────────

class InstrumentSyncLog(Base):
    __tablename__ = "instrument_sync_logs"

    id                      = Column(Integer, primary_key=True, autoincrement=True)
    source                  = Column(Enum(PriceSource), nullable=False)
    instruments_upserted    = Column(Integer, nullable=False, default=0)
    instruments_deactivated = Column(Integer, nullable=False, default=0)
    error                   = Column(Text, nullable=True)
    synced_at               = Column(DateTime(timezone=True), server_default=func.now())
