# =============================================================================
# app/models/portfolio.py
# Module 8 — Position & Portfolio | ARC Trading Platform
# =============================================================================
#
# DATABASE TABLES:
#   holdings_snapshots   — Point-in-time snapshot of delivery holdings
#   portfolio_snapshots  — Daily portfolio value snapshots for history charts
#
# NOTE: The core Position model and TradeLog model were created in Module 7
#       (app/models/position.py and app/models/fill.py).
#       This file adds the snapshot tables needed by Module 8.
#
# WHY SNAPSHOTS?
#   Live data (positions, wallet) is always computed in real-time.
#   Snapshots are taken at end-of-day to power:
#     - "Portfolio value over time" charts
#     - "Day P&L" calculations (today's value - yesterday's snapshot)
#     - Historical performance reports (Module 14)
# =============================================================================

from __future__ import annotations

import enum
from datetime import datetime, timezone, date
from decimal import Decimal

from sqlalchemy import (
    Date, DateTime, Enum, ForeignKey,
    Integer, Numeric, String, Text
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class HoldingsSnapshot(Base):
    """
    Point-in-time snapshot of a user's DELIVERY holdings.

    DIFFERENCE between positions and holdings:
        positions       → intraday + delivery, tracks live net_qty
        holdings        → delivery only, long-term overnight positions

    A new row is inserted every day at EOD for each delivery position.
    Used for the Holdings tab and portfolio history charts.
    """
    __tablename__ = "holdings_snapshots"

    id           : Mapped[int]     = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id      : Mapped[int]     = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True
    )
    snapshot_date: Mapped[date]    = mapped_column(
        Date, nullable=False, index=True,
        comment="The date this snapshot was taken — one row per user per instrument per day"
    )

    # What instrument
    instrument_id: Mapped[str]     = mapped_column(String(36), nullable=False)
    symbol       : Mapped[str]     = mapped_column(String(100), nullable=False)
    exchange     : Mapped[str]     = mapped_column(String(20), nullable=False, default="NSE")
    instrument_type: Mapped[str]   = mapped_column(
        String(20), nullable=False, default="equity",
        comment="equity, etf, futures, options, crypto — determines PnL calculation method"
    )

    # Quantity — Numeric(18,8) supports crypto fractional quantities like 0.00001 BTC
    quantity     : Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False,
        comment="Delivery quantity held overnight. Supports crypto decimals."
    )
    avg_cost     : Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False,
        comment="Weighted average buy price per unit at snapshot time"
    )

    # Values at snapshot time
    close_price  : Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True,
        comment="Closing price at EOD when snapshot was taken"
    )
    market_value : Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2), nullable=True,
        comment="quantity × close_price at snapshot time"
    )
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2), nullable=True,
        comment="(close_price - avg_cost) × quantity at snapshot time"
    )
    pnl_pct      : Mapped[Decimal | None] = mapped_column(
        Numeric(8, 4), nullable=True,
        comment="unrealized_pnl / (avg_cost × quantity) × 100"
    )

    created_at   : Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    def __repr__(self) -> str:
        return f"<HoldingsSnapshot user={self.user_id} {self.symbol} qty={self.quantity} date={self.snapshot_date}>"


class PortfolioSnapshot(Base):
    """
    Daily portfolio value snapshot — one row per user per day.

    USED FOR:
        - "Portfolio value over time" equity curve chart
        - Calculating XIRR / CAGR (future analytics module)
        - Day P&L = today's equity - yesterday's snapshot equity

    HOW IT WORKS:
        A background job runs at 3:30 PM IST every trading day.
        It sums up: cash + position values + holdings values.
        Stores the total as today's snapshot.
    """
    __tablename__ = "portfolio_snapshots"

    id            : Mapped[int]     = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id       : Mapped[int]     = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True
    )
    snapshot_date : Mapped[date]    = mapped_column(Date, nullable=False, index=True)

    # The 7 portfolio output metrics from spec section 5.8
    cash_balance  : Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0"),
        comment="Free wallet cash at EOD"
    )
    margin_used   : Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0"),
        comment="Total margin blocked by open orders"
    )
    position_value: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0"),
        comment="Market value of all open positions at EOD close"
    )
    holdings_value: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0"),
        comment="Market value of all delivery holdings at EOD close"
    )
    realized_pnl  : Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0"),
        comment="Cumulative realized PnL from all closed trades to date"
    )
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0"),
        comment="Unrealized PnL on open positions at EOD"
    )
    total_equity  : Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0"),
        comment="cash + position_value + holdings_value + unrealized_pnl"
    )
    day_pnl       : Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0"),
        comment="today's equity - yesterday's snapshot equity"
    )

    created_at    : Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    def __repr__(self) -> str:
        return f"<PortfolioSnapshot user={self.user_id} date={self.snapshot_date} equity={self.total_equity}>"
