# =============================================================================
# app/models/fill.py
# Module 7 — Execution Engine | ARC Trading Platform
# =============================================================================
#
# DATABASE TABLES:
#   order_fills       — One row per fill event (partial or full fill)
#   trade_logs        — Immutable trade history — one row per completed trade
#   execution_events  — Audit log for every execution engine action
#
# OPTIMISTIC LOCKING on the Order model:
#   We add a `version` column to the orders table.
#   Before updating an order, we check the version hasn't changed.
#   If another worker already updated it, we get a conflict and skip.
#   This prevents TWO workers from filling the SAME order simultaneously.
#
# HOW OPTIMISTIC LOCKING WORKS (beginner explanation):
#
#   Worker A reads order #5 → version=1
#   Worker B reads order #5 → version=1
#
#   Worker A tries to fill:
#     UPDATE orders SET status='filled', version=2 WHERE id=5 AND version=1
#     → 1 row updated ✓ (version was still 1)
#
#   Worker B tries to fill (too late):
#     UPDATE orders SET status='filled', version=2 WHERE id=5 AND version=1
#     → 0 rows updated ✗ (version is now 2, not 1)
#     → Worker B detects 0 rows → skips → no duplicate fill
# =============================================================================

from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime, Enum, ForeignKey, Index,
    Integer, Numeric, String, Text, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.order import Order
    from app.models.user import User


# =============================================================================
# SECTION 1 — ENUMERATIONS
# =============================================================================

class FillType(str, enum.Enum):
    """How this fill was created."""
    MARKET_IMMEDIATE = "market_immediate"  # Market order filled instantly
    LIMIT_TRIGGERED  = "limit_triggered"   # Limit watcher triggered fill
    SL_TRIGGERED     = "sl_triggered"      # Stop-loss watcher triggered fill
    TP_TRIGGERED     = "tp_triggered"      # Take-profit watcher triggered fill
    MANUAL           = "manual"            # Admin-forced fill


class TradeType(str, enum.Enum):
    """Direction of the completed trade."""
    BUY  = "buy"
    SELL = "sell"


class ExecutionEventType(str, enum.Enum):
    """Types of events the execution engine logs."""
    FILL_ATTEMPTED  = "fill_attempted"   # Worker tried to fill
    FILL_SUCCESS    = "fill_success"     # Fill completed successfully
    FILL_SKIPPED    = "fill_skipped"     # Skipped due to idempotency key
    FILL_FAILED     = "fill_failed"      # Fill failed with error
    STALE_QUOTE     = "stale_quote"      # Quote was too old — rejected
    VERSION_CONFLICT= "version_conflict" # Optimistic lock conflict — another worker won
    LIMIT_CHECKED   = "limit_checked"    # Limit watcher checked but price not met


# =============================================================================
# SECTION 2 — ORDER FILL TABLE
# =============================================================================

class OrderFill(Base):
    """
    One row per fill event.

    A single order can have MULTIPLE fills (partial fills).
    Example: Order for 100 shares → filled 40 at 9:15 AM, 60 at 9:16 AM.

    IDEMPOTENCY:
        idempotency_key is UNIQUE in the database.
        If the worker retries, it tries to INSERT with the same key.
        The INSERT fails with a unique constraint violation.
        The worker catches this → knows fill already happened → skips.
        This guarantees NO DUPLICATE FILLS ever.
    """
    __tablename__ = "order_fills"

    id     : Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Which order this fill belongs to
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=False, index=True
    )

    # Who this fill belongs to (denormalized for fast portfolio queries)
    user_id : Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True
    )

    # What was filled
    symbol       : Mapped[str]     = mapped_column(String(100), nullable=False)
    instrument_id: Mapped[str]     = mapped_column(String(36),  nullable=False)
    fill_type    : Mapped[FillType]= mapped_column(Enum(FillType), nullable=False)

    # Fill quantities and price
    fill_qty  : Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False,
        comment="How many units were filled in this event"
    )
    fill_price: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False,
        comment="The price at which this fill executed — actual LTP from Redis"
    )
    fill_value: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False,
        comment="fill_qty × fill_price — total value of this fill"
    )

    # Brokerage/fees on this fill
    brokerage : Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal("20.00"), nullable=False,
        comment="Flat ₹20 per executed order — or 0.03% of fill_value (whichever lower)"
    )

    # IDEMPOTENCY KEY — prevents duplicate fills on worker retry
    # Format: "fill_{order_id}_{fill_sequence_number}"
    # The UNIQUE constraint is the actual guard — not application code.
    idempotency_key: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True,
        comment="Unique key per fill — INSERT fails if already exists = no duplicate"
    )

    # Which version of the order was seen when this fill was created
    # Used to verify optimistic lock was respected
    order_version_at_fill: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="The order.version value at the time of fill — for audit"
    )

    filled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    # Relationships
    order: Mapped[Order] = relationship("Order")

    def __repr__(self) -> str:
        return (
            f"<OrderFill id={self.id} order_id={self.order_id} "
            f"qty={self.fill_qty} price={self.fill_price}>"
        )


# =============================================================================
# SECTION 3 — TRADE LOG TABLE (Immutable History)
# =============================================================================

class TradeLog(Base):
    """
    Immutable trade history — one row per completed fill.
    Per spec section 5.8: 'Trade history timeline.'

    NEVER update or delete these rows.
    They are the permanent record of every trade that happened.
    Used for portfolio PnL calculations and regulatory reporting.
    """
    __tablename__ = "trade_logs"

    id      : Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fill_id : Mapped[int] = mapped_column(
        Integer, ForeignKey("order_fills.id", ondelete="RESTRICT"),
        nullable=False, unique=True,  # One trade log per fill
        index=True
    )
    order_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    user_id : Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    # Trade details
    symbol       : Mapped[str]      = mapped_column(String(100), nullable=False)
    instrument_id: Mapped[str]      = mapped_column(String(36),  nullable=False)
    trade_type   : Mapped[TradeType]= mapped_column(Enum(TradeType), nullable=False)
    product_type : Mapped[str]      = mapped_column(String(20),  nullable=False)
    exchange     : Mapped[str]      = mapped_column(String(20),  nullable=False, default="NSE")

    # Trade quantities and pricing
    quantity    : Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    price       : Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    trade_value : Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    brokerage   : Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    net_value   : Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False,
        comment="trade_value - brokerage for sells, trade_value + brokerage for buys"
    )

    # PnL fields — computed at trade time for BUY→SELL pairs
    # For a BUY trade: realized_pnl = 0 (position opened, not closed)
    # For a SELL trade: realized_pnl = (sell_price - avg_buy_price) × qty - fees
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal("0"), nullable=False
    )
    avg_cost_at_trade: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True,
        comment="The avg_cost from positions table at the time this trade executed"
    )

    traded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True
    )

    def __repr__(self) -> str:
        return (
            f"<TradeLog id={self.id} user={self.user_id} "
            f"{self.trade_type.value} {self.quantity} {self.symbol} @ {self.price}>"
        )


# =============================================================================
# SECTION 4 — EXECUTION EVENT TABLE (Engine Audit Log)
# =============================================================================

class ExecutionEvent(Base):
    """
    Audit log for every action taken by the Execution Engine.
    Records both successes and failures — essential for debugging.

    Per spec section 5.7: 'Emit order status events over WebSocket.'
    This table is the persistent version of those events.
    """
    __tablename__ = "execution_events"

    id      : Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    user_id : Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    event_type: Mapped[ExecutionEventType] = mapped_column(
        Enum(ExecutionEventType), nullable=False
    )

    # What the engine saw
    symbol         : Mapped[str]          = mapped_column(String(100), nullable=False)
    ltp_at_event   : Mapped[Decimal|None] = mapped_column(Numeric(18, 8), nullable=True)
    quote_age_secs : Mapped[float|None]   = mapped_column(
        Numeric(8, 3), nullable=True,
        comment="How old the price quote was in seconds when engine ran"
    )

    # Result
    success        : Mapped[bool]         = mapped_column(
        Integer, nullable=False, default=False
    )
    message        : Mapped[str|None]     = mapped_column(Text, nullable=True)
    fill_id        : Mapped[int|None]     = mapped_column(Integer, nullable=True)

    # Worker info
    worker_name    : Mapped[str]          = mapped_column(
        String(50), nullable=False, default="execution_engine"
    )
    idempotency_key: Mapped[str|None]     = mapped_column(String(100), nullable=True)

    created_at     : Mapped[datetime]     = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<ExecutionEvent order={self.order_id} "
            f"type={self.event_type.value} success={self.success}>"
        )
