# =============================================================================
# app/services/portfolio/position_builder.py
# Submodule: PositionBuilder
#
# PURPOSE:
#   Maintains the Position table in real-time as fills come in.
#   Called by the Execution Engine after every fill.
#
# KEY CONCEPT — WEIGHTED AVERAGE COST POLICY:
#   When a user buys more of something they already hold,
#   the avg_cost is recalculated using the weighted average formula.
#
#   FORMULA:
#     new_avg = (existing_qty × old_avg) + (new_qty × new_price)
#               ─────────────────────────────────────────────────
#                        (existing_qty + new_qty)
#
#   EXAMPLE:
#     Day 1: Buy 10 RELIANCE @ ₹2,400  →  avg_cost = 2,400.00
#     Day 2: Buy 5  RELIANCE @ ₹2,500  →  avg_cost = (10×2400 + 5×2500) / 15
#                                                   = (24000 + 12500) / 15
#                                                   = 36500 / 15
#                                                   = ₹2,433.33
#
#   NOTE: avg_cost only changes on BUY fills.
#         SELL fills reduce net_qty but avg_cost stays the same.
#         Realized PnL = (sell_price - avg_cost) × qty_sold - brokerage
#
# ASSET TYPE AWARENESS:
#   Equity   → delivery + intraday, avg_cost in ₹/share
#   F&O      → lot-based, premium tracking for options
#   Crypto   → fractional quantities (0.00001 BTC supported)
# =============================================================================

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def get_or_create_position(
    db            : Session,
    user_id       : int,
    instrument_id : str,
    symbol        : str,
    exchange      : str,
    product_type  : str,
) -> object:
    """
    Find an existing position for this user+instrument+product_type,
    or create a new one with zero quantities.

    Uses SELECT FOR UPDATE to lock the row during the fill transaction,
    preventing race conditions when two fills arrive simultaneously.
    """
    from app.models.position import Position, PositionSide

    # Try to find existing position
    position = db.execute(
        select(Position).where(
            Position.user_id       == user_id,
            Position.instrument_id == instrument_id,
            Position.product_type  == product_type,
        ).with_for_update()   # Lock the row — no other transaction can update it
    ).scalars().first()

    if position is None:
        # First time trading this instrument — create a fresh position
        position = Position(
            user_id       = user_id,
            instrument_id = instrument_id,
            symbol        = symbol,
            exchange      = exchange,
            product_type  = product_type,
            net_qty       = Decimal("0"),
            buy_qty       = Decimal("0"),
            sell_qty      = Decimal("0"),
            avg_cost      = Decimal("0"),
            realized_pnl  = Decimal("0"),
            total_brokerage = Decimal("0"),
            is_open       = True,
            side          = "long",
            day_buy_qty   = Decimal("0"),
            day_sell_qty  = Decimal("0"),
            day_buy_value = Decimal("0"),
            day_pnl       = Decimal("0"),
        )
        db.add(position)
        db.flush()  # get position.id without committing
        logger.info("New position created: user=%s %s %s", user_id, symbol, product_type)

    return position


def apply_buy_fill(
    position   : object,
    fill_qty   : Decimal,
    fill_price : Decimal,
    brokerage  : Decimal,
    now        : datetime,
) -> None:
    """
    Apply a BUY fill to a position using Weighted Average Cost policy.

    STEP 1: Calculate new weighted average cost
        Formula: new_avg = (old_qty × old_avg + fill_qty × fill_price)
                           ─────────────────────────────────────────────
                                    (old_qty + fill_qty)

    STEP 2: Update cumulative quantities

    STEP 3: Update day totals (reset at midnight)

    Args:
        position   : The Position object (already locked with FOR UPDATE)
        fill_qty   : How many units were bought in this fill
        fill_price : The price at which the fill executed
        brokerage  : Fee charged for this fill
        now        : Timestamp of the fill
    """
    # ── STEP 1: Recalculate weighted average cost ─────────────────────────────
    # This is the most important calculation in the portfolio module.
    # It correctly accounts for multiple buy fills at different prices.
    if position.net_qty <= 0:
        # First buy — avg cost is simply the fill price
        position.avg_cost = fill_price
    else:
        # Weighted average of existing holding + new fill
        old_value  = position.net_qty * position.avg_cost   # what was paid before
        new_value  = fill_qty * fill_price                   # what we're paying now
        total_qty  = position.net_qty + fill_qty
        # New avg = total money spent / total units held
        position.avg_cost = (old_value + new_value) / total_qty

    # ── STEP 2: Update quantities ────────────────────────────────────────────
    position.net_qty     += fill_qty    # net holding increases
    position.buy_qty     += fill_qty    # cumulative buys (never decreases)
    position.is_open      = True
    position.side         = "long" if position.net_qty >= 0 else "short"

    # ── STEP 3: Update day totals ────────────────────────────────────────────
    fill_value = fill_qty * fill_price
    position.day_buy_qty   = (position.day_buy_qty or Decimal("0")) + fill_qty
    position.day_buy_value = (position.day_buy_value or Decimal("0")) + fill_value

    # ── STEP 4: Accumulate brokerage ────────────────────────────────────────
    position.total_brokerage = (position.total_brokerage or Decimal("0")) + brokerage

    # ── STEP 5: Set timestamps ───────────────────────────────────────────────
    if not position.first_buy_at:
        position.first_buy_at = now
    position.last_trade_at = now
    position.updated_at    = now

    logger.debug(
        "BUY fill applied: %s qty=%s @ %s → new_avg=%s net_qty=%s",
        position.symbol, fill_qty, fill_price,
        round(position.avg_cost, 4), position.net_qty
    )


def apply_sell_fill(
    position   : object,
    fill_qty   : Decimal,
    fill_price : Decimal,
    brokerage  : Decimal,
    now        : datetime,
) -> Decimal:
    """
    Apply a SELL fill to a position. Returns the realized PnL for this sell.

    REALIZED PnL FORMULA:
        realized_pnl = (sell_price - avg_cost) × qty_sold - brokerage

    IMPORTANT: avg_cost does NOT change on sells.
    Only BUY fills change the avg_cost (weighted average policy).

    EXAMPLE:
        avg_cost    = ₹2,433.33  (from previous buys)
        sell_price  = ₹2,600.00
        sell_qty    = 5 shares
        brokerage   = ₹20.00

        realized_pnl = (2600 - 2433.33) × 5 - 20
                     = 166.67 × 5 - 20
                     = 833.35 - 20
                     = ₹813.35 profit on this sell

    ASSET TYPE NOTES:
        Equity  : straightforward formula above
        F&O     : for options BUY, the premium paid IS the avg_cost
                  realized_pnl = (sell_premium - buy_premium) × lot_size
        Crypto  : same formula but qty can be fractional (0.001 BTC)

    Args:
        position   : The Position object (already locked)
        fill_qty   : Units sold in this fill
        fill_price : Price at which sold
        brokerage  : Fee charged for this fill
        now        : Timestamp

    Returns:
        realized_pnl: The profit or loss made on this sell trade
    """
    # ── STEP 1: Calculate realized PnL ──────────────────────────────────────
    # This is the money you actually made (or lost) by closing this position
    profit_per_unit = fill_price - position.avg_cost   # positive = profit
    gross_pnl       = profit_per_unit * fill_qty        # before fees
    realized_pnl    = gross_pnl - brokerage             # after fees

    # ── STEP 2: Accumulate realized PnL on the position ──────────────────────
    position.realized_pnl    = (position.realized_pnl or Decimal("0")) + realized_pnl
    position.total_brokerage = (position.total_brokerage or Decimal("0")) + brokerage
    position.day_pnl         = (position.day_pnl or Decimal("0")) + realized_pnl

    # ── STEP 3: Reduce net quantity ──────────────────────────────────────────
    position.net_qty  -= fill_qty       # holding decreases
    position.sell_qty  = (position.sell_qty or Decimal("0")) + fill_qty

    # ── STEP 4: Day sell tracking ────────────────────────────────────────────
    position.day_sell_qty = (position.day_sell_qty or Decimal("0")) + fill_qty

    # ── STEP 5: Update position state ────────────────────────────────────────
    if position.net_qty <= 0:
        position.is_open = False    # position fully closed
        position.net_qty = Decimal("0")   # prevent negative from rounding
    position.side = "long" if position.net_qty >= 0 else "short"
    position.last_trade_at = now
    position.updated_at    = now

    logger.debug(
        "SELL fill applied: %s qty=%s @ %s → realized_pnl=%s net_qty=%s",
        position.symbol, fill_qty, fill_price,
        round(realized_pnl, 2), position.net_qty
    )

    return realized_pnl
