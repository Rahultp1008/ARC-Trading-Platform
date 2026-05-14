# =============================================================================
# app/models/position.py
# Module 7 — Execution Engine | ARC Trading Platform
# =============================================================================
#
# DATABASE TABLE: positions
#
# PURPOSE:
#   Tracks the CURRENT holding of every user for every instrument.
#   One row per (user_id, instrument_id, product_type) combination.
#
# KEY CONCEPT — WEIGHTED AVERAGE COST:
#   Every time a user BUYS more shares, avg_cost is recalculated:
#
#   Example:
#     Day 1: Buy 10 RELIANCE @ ₹2400 → avg_cost = 2400.00
#     Day 2: Buy 5  RELIANCE @ ₹2500 → avg_cost = (10×2400 + 5×2500) / 15
#                                              = (24000 + 12500) / 15
#                                              = 36500 / 15
#                                              = ₹2433.33
#
#   When user SELLS shares:
#     Realized PnL = (sell_price - avg_cost) × qty_sold - brokerage
#     net_qty -= qty_sold
#     avg_cost stays the same (only changes on new buys)
#
# UNREALIZED PnL (computed at read time, not stored):
#   unrealized_pnl = (current_LTP - avg_cost) × net_qty
#   This is calculated live using the Redis price cache.
# =============================================================================

from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime, Enum, ForeignKey, Index,
    Integer, Numeric, String, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.user import User


class PositionSide(str, enum.Enum):
    """Current direction of the position."""
    LONG  = "long"   # Net qty > 0 — user holds shares
    SHORT = "short"  # Net qty < 0 — user has sold short


class Position(Base):
    """
    Current open position for a user on one instrument.

    UNIQUE CONSTRAINT:
        (user_id, instrument_id, product_type) — one row per combination.
        A user can have RELIANCE as INTRADAY and also as DELIVERY separately.

    UPDATES:
        This row is updated on EVERY fill:
        - BUY fill  → net_qty increases, avg_cost recalculated
        - SELL fill → net_qty decreases, realized_pnl updated
        - When net_qty reaches 0 → position is CLOSED (is_open=False)
    """
    __tablename__ = "positions"
    __table_args__ = (
        # Ensure one position per user per instrument per product type
        UniqueConstraint(
            "user_id", "instrument_id", "product_type",
            name="uq_position_user_instrument_product"
        ),
    )

    id           : Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Who owns this position
    user_id      : Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True
    )

    # What instrument
    instrument_id: Mapped[str] = mapped_column(String(36),  nullable=False, index=True)
    symbol       : Mapped[str] = mapped_column(String(100), nullable=False)
    exchange     : Mapped[str] = mapped_column(String(20),  nullable=False, default="NSE")
    product_type : Mapped[str] = mapped_column(
        String(20), nullable=False,
        comment="intraday, delivery, futures, options — determines square-off rules"
    )

    # ── Quantity tracking ─────────────────────────────────────────────────────
    net_qty: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal("0"),
        comment="Current net quantity. Positive=long, Negative=short, 0=closed"
    )
    buy_qty: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal("0"),
        comment="Total bought qty (cumulative, never decreases)"
    )
    sell_qty: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal("0"),
        comment="Total sold qty (cumulative, never decreases)"
    )

    # ── Cost tracking ─────────────────────────────────────────────────────────
    avg_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal("0"),
        comment="Weighted average buy cost per unit — recalculated on every BUY fill"
    )

    # ── PnL tracking ─────────────────────────────────────────────────────────
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0"),
        comment="Total profit/loss from all CLOSED (sold) portions of this position"
    )
    total_brokerage: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0"),
        comment="Total fees paid on this position (buys + sells)"
    )

    # ── Position state ────────────────────────────────────────────────────────
    is_open: Mapped[bool] = mapped_column(
        Integer, nullable=False, default=True,
        comment="True if net_qty > 0. Set to False when position fully closed."
    )
    side: Mapped[PositionSide] = mapped_column(
        Enum(PositionSide), nullable=False, default=PositionSide.LONG
    )

    # ── Day tracking (reset at EOD) ───────────────────────────────────────────
    day_buy_qty  : Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"), nullable=False)
    day_sell_qty : Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"), nullable=False)
    day_buy_value: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"), nullable=False)
    day_pnl      : Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"), nullable=False)

    # ── Timestamps ───────────────────────────────────────────────────────────
    first_buy_at : Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_trade_at: Mapped[datetime]      = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )
    updated_at   : Mapped[datetime]      = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<Position user={self.user_id} {self.symbol} "
            f"qty={self.net_qty} avg={self.avg_cost} pnl={self.realized_pnl}>"
        )

    def recalculate_avg_cost(self, new_qty: Decimal, new_price: Decimal) -> None:
        """
        Recalculate weighted average cost after a BUY fill.

        Formula:
            new_avg = (existing_qty × old_avg + new_qty × new_price)
                      ─────────────────────────────────────────────
                              (existing_qty + new_qty)

        Called by the Execution Engine INSIDE the DB transaction.
        """
        if self.net_qty <= 0:
            # First buy — avg cost is simply the fill price
            self.avg_cost = new_price
        else:
            # Weighted average of existing + new
            old_value  = self.net_qty * self.avg_cost
            new_value  = new_qty * new_price
            total_qty  = self.net_qty + new_qty
            self.avg_cost = (old_value + new_value) / total_qty

    def apply_sell(self, sell_qty: Decimal, sell_price: Decimal, brokerage: Decimal) -> Decimal:
        """
        Apply a SELL fill to this position. Returns realized PnL for this sell.

        Formula:
            realized_pnl = (sell_price - avg_cost) × sell_qty - brokerage

        Called by the Execution Engine INSIDE the DB transaction.
        avg_cost does NOT change on sells — only on new buys.
        """
        pnl = (sell_price - self.avg_cost) * sell_qty - brokerage
        self.realized_pnl += pnl
        return pnl
