# =============================================================================
# app/models/order.py
# Module 6 — Order Management | ARC Trading Platform
# MODIFIED for Module 7 — Added version column for optimistic locking
# =============================================================================
#
# DATABASE TABLES CREATED BY THIS FILE:
#   orders        — The central order record. One row per user order.
#   order_events  — Immutable state transition audit log.
#   order_triggers— SL/TP price trigger instructions attached to orders.
#
# ORDER LIFECYCLE (how a state moves from 'created' → 'accepted'):
#
#   1. User calls POST /orders                    → status = CREATED
#   2. Validation service runs all 10 checks      → status = PENDING_VALIDATION
#   3. All checks pass, margin is blocked         → status = ACCEPTED
#   4a. Order type = MARKET  → goes straight to Execution Engine → FILLED
#   4b. Order type = LIMIT   → sits in queue waiting for LTP     → QUEUED
#   4c. Order type = SL/TP   → sits waiting for trigger price    → PENDING_TRIGGER
#
# Each transition writes a row to order_events for full traceability.
#
# ── MODULE 7 CHANGE — OPTIMISTIC LOCKING ─────────────────────────────────────
# Added: version column on the Order model.
#
# WHAT IS OPTIMISTIC LOCKING?
#   When the Limit Order Watcher runs in the background, it processes
#   QUEUED orders every second. If two workers run at the same time,
#   both might try to fill the same order simultaneously.
#
#   Without locking → DUPLICATE FILL → user gets charged twice → data corruption.
#   With version   → only ONE worker wins → other sees version changed → skips.
#
# HOW VERSION WORKS (step by step):
#
#   Order #5 created → version = 0
#
#   Worker A reads order #5 → sees version = 0
#   Worker B reads order #5 → sees version = 0
#
#   Worker A executes:
#     UPDATE orders
#     SET status = 'filled', version = 1      ← bumps version to 1
#     WHERE id = 5 AND version = 0            ← checks version is still 0
#     → 1 row updated ✓  (version was 0, now set to 1)
#
#   Worker B executes (too late):
#     UPDATE orders
#     SET status = 'filled', version = 1
#     WHERE id = 5 AND version = 0            ← version is now 1, not 0
#     → 0 rows updated ✗  (version mismatch — another worker already filled it)
#     → Worker B detects 0 rows → SKIPS → no duplicate fill
#
# This is called "optimistic" because we assume conflicts are rare,
# so we do NOT lock the row upfront. We only detect conflicts at write time.
# =============================================================================

from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean, DateTime, Enum, ForeignKey,
    Integer, Numeric, String, Text, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.user import User


# =============================================================================
# SECTION 1 — ENUMERATIONS
# =============================================================================

class OrderType(str, enum.Enum):
    """
    Per spec section 5.6 — Order Types.

    The spec also lists future extensibility for bracket/GTT/OCO orders.
    We add them as commented-out values so the schema is ready when needed —
    simply uncomment and run a DB migration.
    """
    MARKET           = "market"           # Fill immediately at current market price
    LIMIT            = "limit"            # Fill only when LTP reaches the limit price
    STOP_LOSS_MARKET = "stop_loss_market" # Trigger a market order when SL price is hit
    STOP_LOSS_LIMIT  = "stop_loss_limit"  # Trigger a limit order when SL price is hit
    TAKE_PROFIT      = "take_profit"      # Close position when profit target is reached

    # ── Future extensibility (per spec) — uncomment + migrate when ready ──
    # BRACKET = "bracket"  # Entry + SL + TP as a single bundled order
    # GTT     = "gtt"      # Good Till Triggered — persists across market sessions
    # OCO     = "oco"      # One Cancels Other — two linked orders


class OrderSide(str, enum.Enum):
    """Direction of the trade."""
    BUY  = "buy"
    SELL = "sell"


class ProductType(str, enum.Enum):
    """
    The holding type for this order — determines margin, square-off rules.
    Per spec section 5.9 — Risk & Margin.
    """
    INTRADAY = "intraday"   # MIS — must be squared off same day by 3:20 PM
    DELIVERY = "delivery"   # CNC — can hold overnight (equities only)
    FUTURES  = "futures"    # F&O futures contract
    OPTIONS  = "options"    # F&O options contract (buy side = full premium)


class OrderStatus(str, enum.Enum):
    """
    Per spec section 5.6 — 11 states covering the complete order lifecycle.

    State machine:
        CREATED ──────────────→ PENDING_VALIDATION
        PENDING_VALIDATION ───→ REJECTED  (if any validation fails)
        PENDING_VALIDATION ───→ ACCEPTED  (all validations pass)
        ACCEPTED ─────────────→ QUEUED         (limit orders)
        ACCEPTED ─────────────→ PENDING_TRIGGER (SL/TP orders)
        ACCEPTED ─────────────→ FILLED         (market orders — immediate)
        QUEUED ───────────────→ PARTIALLY_FILLED (some qty filled)
        QUEUED ───────────────→ FILLED          (all qty filled)
        QUEUED ───────────────→ CANCELLED       (user cancels)
        QUEUED ───────────────→ EXPIRED         (end of day, GTT expiry)
        ANY OPEN STATUS ──────→ FAILED          (system error during execution)
    """
    CREATED           = "created"           # Order received, not yet validated
    PENDING_VALIDATION= "pending_validation"# Validation checks running
    REJECTED          = "rejected"          # Failed one or more validations
    ACCEPTED          = "accepted"          # Passed all validations, margin blocked
    QUEUED            = "queued"            # Waiting for limit price to be hit
    PENDING_TRIGGER   = "pending_trigger"   # Waiting for SL/TP trigger price
    PARTIALLY_FILLED  = "partially_filled"  # Some shares filled, rest pending
    FILLED            = "filled"            # All quantity filled — order complete
    CANCELLED         = "cancelled"         # Cancelled by user or auto square-off
    EXPIRED           = "expired"           # Not filled before expiry (end of day)
    FAILED            = "failed"            # System error during execution


class TriggerSide(str, enum.Enum):
    """
    Which direction the price must move to fire the trigger.

    For a BUY position:
        SL trigger = BELOW (protect from price falling)
        TP trigger = ABOVE (close when price rises to target)

    For a SELL position (short):
        SL trigger = ABOVE (protect from price rising)
        TP trigger = BELOW (close when price falls to target)
    """
    ABOVE = "above"  # Fire when LTP crosses ABOVE trigger_price
    BELOW = "below"  # Fire when LTP crosses BELOW trigger_price


# =============================================================================
# SECTION 2 — ORDER TABLE
# =============================================================================

class Order(Base):
    """
    Central order record — one row per order placed by a user.

    IMPORTANT DESIGN DECISION:
        instrument_id is stored as UUID(as_uuid=False) which maps to
        TEXT/VARCHAR in Postgres, matching the UUID stored in instruments.id
        without a foreign key constraint (FK skipped because instruments
        can be synced/replaced and we want order history to survive).

    MARGIN LIFECYCLE:
        When order moves ACCEPTED → margin_blocked is set.
        When order moves FILLED  → margin_blocked is released by Execution Engine.
        When order CANCELLED     → Cancellation Engine releases margin_blocked.

    VERSION (Module 7):
        Starts at 0 when order is created.
        Incremented by 1 on every fill by the Execution Engine.
        Used for optimistic locking to prevent duplicate fills.
    """
    __tablename__ = "orders"

    # ── Primary key ──────────────────────────────────────────────────────────
    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, index=True
    )

    # ── Who placed the order ─────────────────────────────────────────────────
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="Must be an active, KYC-approved user"
    )

    # ── What instrument ──────────────────────────────────────────────────────
    instrument_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        index=True,
        comment="UUID from instruments table — stored as string for resilience"
    )
    symbol: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="Denormalized symbol e.g. NSE:RELIANCE for fast display"
    )

    # ── Order classification ─────────────────────────────────────────────────
    order_type  : Mapped[OrderType]   = mapped_column(Enum(OrderType),   nullable=False)
    side        : Mapped[OrderSide]   = mapped_column(Enum(OrderSide),   nullable=False)
    product_type: Mapped[ProductType] = mapped_column(Enum(ProductType), nullable=False)

    # ── Quantity fields ──────────────────────────────────────────────────────
    quantity  : Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False,
        comment="Total ordered quantity — must be > 0 and F&O lot size multiple"
    )
    filled_qty: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0"), nullable=False,
        comment="Quantity filled so far — updated by Execution Engine on each fill"
    )

    # ── Price fields ─────────────────────────────────────────────────────────
    price       : Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True,
        comment="Limit price — required for LIMIT and SL_LIMIT orders"
    )
    trigger_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True,
        comment="SL/TP activation price — required for SL and TAKE_PROFIT orders"
    )
    average_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True,
        comment="Weighted average fill price — computed by Execution Engine"
    )

    # ── Attached SL/TP prices ─────────────────────────────────────────────────
    sl_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True,
        comment="Stop-loss price — generates an OrderTrigger row on acceptance"
    )
    tp_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True,
        comment="Take-profit price — generates an OrderTrigger row on acceptance"
    )

    # ── Current state ────────────────────────────────────────────────────────
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus),
        default=OrderStatus.CREATED,
        nullable=False,
        index=True
    )
    rejection_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="Human-readable reason — set when status = REJECTED"
    )

    # ── Margin tracking ──────────────────────────────────────────────────────
    margin_required: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal("0"), nullable=False,
        comment="Margin calculated during validation — snapshot at order time"
    )
    margin_blocked: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal("0"), nullable=False,
        comment="Currently blocked from user's free balance — zeroed on fill/cancel"
    )

    # ── Idempotency ──────────────────────────────────────────────────────────
    client_order_id: Mapped[str | None] = mapped_column(
        String(100), unique=True, nullable=True,
        comment="Client-provided unique key — prevents duplicate orders on retry"
    )

    # =========================================================================
    # ── MODULE 7 ADDITION — OPTIMISTIC LOCKING VERSION ───────────────────────
    # =========================================================================
    # This is the ONLY new field added to this file for Module 7.
    #
    # LIFECYCLE:
    #   Order created           → version = 0   (default)
    #   First fill by engine    → version = 1   (engine sets version += 1)
    #   Partial fill #2         → version = 2
    #   Final fill              → version = 3
    #
    # HOW THE ENGINE USES IT:
    #   # Read the current version
    #   current_version = order.version   # e.g. 0
    #
    #   # Try to update — only succeeds if version has NOT changed
    #   rows_updated = db.execute(
    #       update(Order)
    #       .where(Order.id == order_id)
    #       .where(Order.version == current_version)   # ← the lock check
    #       .values(status="filled", version=current_version + 1)
    #   ).rowcount
    #
    #   if rows_updated == 0:
    #       # Another worker already filled it — skip to avoid duplicate
    #       return
    #
    # ALSO REQUIRED: Run this SQL once in psql or pgAdmin:
    #   ALTER TABLE orders
    #   ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 0;
    # =========================================================================
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment=(
            "Optimistic lock version — starts at 0 when order is created. "
            "Incremented by 1 on every fill by the Execution Engine. "
            "Used to prevent duplicate fills when multiple workers run simultaneously."
        )
    )
    # =========================================================================

    # ── Timestamps ───────────────────────────────────────────────────────────
    placed_at   : Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )
    accepted_at : Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    filled_at   : Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at  : Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    # ── Relationships ────────────────────────────────────────────────────────
    user: Mapped[User] = relationship("User")
    events: Mapped[list[OrderEvent]] = relationship(
        "OrderEvent",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderEvent.created_at"
    )
    triggers: Mapped[list[OrderTrigger]] = relationship(
        "OrderTrigger",
        back_populates="order",
        cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<Order id={self.id} symbol={self.symbol} "
            f"type={self.order_type.value} status={self.status.value} "
            f"version={self.version}>"
        )


# =============================================================================
# SECTION 3 — ORDER EVENT TABLE (Immutable Audit Trail)
# =============================================================================

class OrderEvent(Base):
    """
    Immutable state-transition log — one row per status change.

    Per spec section 5.11 (Audit Logging):
        'Order placement/cancel/fill/reject events must be captured.'

    RULES:
        - Rows are NEVER updated or deleted after creation.
        - The 'actor' field identifies who/what caused the transition:
            'user'             — user-initiated action (place, cancel)
            'validation_svc'   — automatic validation service
            'execution_engine' — background fill processor
            'sltp_watcher'     — stop-loss/take-profit background worker
            'system'           — generic system action
        - from_status = None means this is the first event (order creation)
    """
    __tablename__ = "order_events"

    id      : Mapped[int]        = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int]        = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False, index=True
    )

    from_status: Mapped[str | None] = mapped_column(
        String(50), nullable=True,
        comment="Status BEFORE this transition — None for the initial 'created' event"
    )
    to_status  : Mapped[str]        = mapped_column(
        String(50), nullable=False,
        comment="Status AFTER this transition"
    )
    reason     : Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="Human-readable explanation — required on REJECTED, CANCELLED"
    )
    actor      : Mapped[str]        = mapped_column(
        String(100), nullable=False, default="system",
        comment="Who caused this transition: user, validation_svc, execution_engine, sltp_watcher"
    )
    actor_id   : Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        comment="user.id if actor is a user, null for system actors"
    )

    # Rich metadata for debugging and compliance
    metadata_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="JSON blob: fill_price, triggered_price, error_code etc."
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    order: Mapped[Order] = relationship("Order", back_populates="events")

    def __repr__(self) -> str:
        return (
            f"<OrderEvent order_id={self.order_id} "
            f"{self.from_status} → {self.to_status} by {self.actor}>"
        )


# =============================================================================
# SECTION 4 — ORDER TRIGGER TABLE (SL/TP Instructions)
# =============================================================================

class OrderTrigger(Base):
    """
    A price trigger instruction attached to an order or position.

    The SL/TP background watcher (spec section 8.2) reads these rows,
    monitors live LTPs from Redis, and fires the trigger when conditions met.

    TRIGGER LOGIC:
        ABOVE trigger fires when: current_LTP >= trigger_price
        BELOW trigger fires when: current_LTP <= trigger_price

    DEDUPLICATION:
        The watcher checks is_triggered=True before executing.
        After executing, it sets is_triggered=True + triggered_at in the
        SAME database transaction as the fill to prevent double-execution.
    """
    __tablename__ = "order_triggers"

    id      : Mapped[int]        = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int]        = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False, index=True
    )

    trigger_type : Mapped[str]          = mapped_column(
        String(20), nullable=False,
        comment="'sl' for stop-loss, 'tp' for take-profit"
    )
    trigger_side : Mapped[TriggerSide]  = mapped_column(
        Enum(TriggerSide), nullable=False,
        comment="ABOVE or BELOW — determines which direction fires the trigger"
    )
    trigger_price: Mapped[Decimal]      = mapped_column(
        Numeric(18, 8), nullable=False,
        comment="The price level that activates this trigger"
    )
    order_type_on_trigger: Mapped[str]  = mapped_column(
        String(30), nullable=False, default="market",
        comment="What order type to place when triggered: market or limit"
    )
    limit_price_on_trigger: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True,
        comment="Limit price to use if order_type_on_trigger = limit"
    )

    # State
    is_triggered: Mapped[bool]          = mapped_column(
        Boolean, default=False, nullable=False,
        comment="True once this trigger has fired — prevents double execution"
    )
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    triggered_at_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True,
        comment="The actual LTP at the moment of trigger firing"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    order: Mapped[Order] = relationship("Order", back_populates="triggers")

    def __repr__(self) -> str:
        return (
            f"<OrderTrigger order_id={self.order_id} "
            f"type={self.trigger_type} side={self.trigger_side.value} "
            f"price={self.trigger_price} fired={self.is_triggered}>"
        )