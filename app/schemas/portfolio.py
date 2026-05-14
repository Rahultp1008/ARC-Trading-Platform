# =============================================================================
# app/schemas/portfolio.py
# Module 8 — Position & Portfolio | ARC Trading Platform
#
# Pydantic v2 schemas for Portfolio API responses.
#
# SCHEMA MAP:
#   PositionResponse      → GET /portfolio/positions  (live positions)
#   HoldingResponse       → GET /portfolio/holdings   (delivery holdings)
#   TradeResponse         → GET /portfolio/trades     (trade history)
#   PnLBreakdownResponse  → GET /portfolio/pnl        (PnL details)
#   PortfolioSummary      → GET /portfolio/summary    (top-level dashboard metrics)
# =============================================================================

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field


# =============================================================================
# POSITION SCHEMAS
# =============================================================================

class PositionResponse(BaseModel):
    """
    A single live open position.
    Unrealized PnL is computed live using Redis LTP at request time.
    """
    id              : int
    user_id         : int
    instrument_id   : str
    symbol          : str
    exchange        : str
    product_type    : str          # intraday, delivery, futures, options

    # Quantity — Decimal(18,8) supports BTC fractional like 0.00001
    net_qty         : Decimal      # positive = long, negative = short
    buy_qty         : Decimal
    sell_qty        : Decimal

    # Cost basis
    avg_cost        : Decimal      # weighted average buy price per unit

    # PnL (computed at response time)
    unrealized_pnl  : Decimal      # (LTP - avg_cost) × net_qty
    realized_pnl    : Decimal      # from closed portions of this position
    pnl_pct         : Decimal      # unrealized_pnl / (avg_cost × net_qty) × 100

    # Live market data
    current_ltp     : Optional[Decimal] = None   # from Redis
    market_value    : Optional[Decimal] = None   # net_qty × current_ltp

    # Day tracking
    day_buy_qty     : Decimal
    day_sell_qty    : Decimal
    day_pnl         : Decimal

    is_open         : bool
    last_trade_at   : datetime

    model_config = ConfigDict(from_attributes=True)


class HoldingResponse(BaseModel):
    """
    A delivery holding (long-term position held overnight).
    Only shows positive net_qty delivery positions.
    """
    instrument_id   : str
    symbol          : str
    exchange        : str
    instrument_type : str          # equity, etf, crypto

    quantity        : Decimal      # units held (Decimal for crypto)
    avg_cost        : Decimal      # weighted average buy price
    current_price   : Optional[Decimal] = None  # live LTP from Redis
    invested_value  : Decimal      # avg_cost × quantity (what you paid)
    current_value   : Optional[Decimal] = None  # current_price × quantity
    unrealized_pnl  : Decimal      # current_value - invested_value
    pnl_pct         : Decimal      # unrealized_pnl / invested_value × 100
    day_change_pct  : Optional[Decimal] = None  # today's price change %

    model_config = ConfigDict(from_attributes=True)


# =============================================================================
# TRADE HISTORY SCHEMAS
# =============================================================================

class TradeResponse(BaseModel):
    """A single executed trade from the trade_logs table."""
    id              : int
    order_id        : int
    symbol          : str
    exchange        : str
    trade_type      : str          # buy or sell
    product_type    : str
    quantity        : Decimal      # Decimal for crypto compatibility
    price           : Decimal      # fill price
    trade_value     : Decimal      # quantity × price
    brokerage       : Decimal      # fee deducted
    net_value       : Decimal      # trade_value ± brokerage
    realized_pnl    : Decimal      # profit/loss on this trade (0 for buys)
    avg_cost_at_trade: Optional[Decimal] = None
    traded_at       : datetime

    model_config = ConfigDict(from_attributes=True)


class TradeListResponse(BaseModel):
    """Paginated trade history."""
    total  : int
    page   : int
    size   : int
    trades : list[TradeResponse]


# =============================================================================
# PnL BREAKDOWN SCHEMA
# =============================================================================

class PnLBreakdownResponse(BaseModel):
    """
    Detailed PnL breakdown per asset class.
    Shows both realized (closed trades) and unrealized (open positions) PnL.
    """
    # Totals
    total_unrealized_pnl: Decimal   # all open positions combined
    total_realized_pnl  : Decimal   # all closed trades combined
    total_pnl           : Decimal   # unrealized + realized
    day_pnl             : Decimal   # today's P&L only

    # By asset class — asset-type aware per spec
    equity_unrealized   : Decimal   # NSE equity positions
    equity_realized     : Decimal
    fno_unrealized      : Decimal   # F&O positions (futures + options)
    fno_realized        : Decimal
    crypto_unrealized   : Decimal   # crypto paper positions
    crypto_realized     : Decimal

    # Brokerage paid
    total_brokerage     : Decimal   # total fees paid across all trades

    model_config = ConfigDict(from_attributes=True)


# =============================================================================
# PORTFOLIO SUMMARY SCHEMA
# =============================================================================

class PortfolioSummary(BaseModel):
    """
    Top-level portfolio summary — the main dashboard widget.
    Per spec section 5.8: all 7 portfolio output metrics.

    HOW EACH IS CALCULATED:
        cash_balance    = SUM(credits) - SUM(debits) from wallet_ledger
        margin_used     = SUM(margin_blocked) from open orders
        free_margin     = cash_balance - margin_used
        position_value  = SUM(net_qty × ltp) for all open positions
        holdings_value  = SUM(quantity × ltp) for delivery holdings
        unrealized_pnl  = SUM((ltp - avg_cost) × net_qty) for all positions
        realized_pnl    = SUM(realized_pnl) from all positions
        total_equity    = cash_balance + position_value + holdings_value + unrealized_pnl
        day_pnl         = total_equity - yesterday_equity_snapshot
    """
    # Per spec section 5.8 — 7 required portfolio outputs
    cash_balance    : Decimal       # free wallet cash
    margin_used     : Decimal       # blocked by open orders
    free_margin     : Decimal       # cash_balance - margin_used
    position_value  : Decimal       # market value of open intraday positions
    holdings_value  : Decimal       # market value of delivery holdings
    unrealized_pnl  : Decimal       # live P&L on open positions (from Redis)
    realized_pnl    : Decimal       # cumulative closed trade P&L
    total_equity    : Decimal       # total portfolio value
    day_pnl         : Decimal       # today's P&L vs yesterday's snapshot
    day_pnl_pct     : Decimal       # day_pnl / yesterday_equity × 100

    # Counts
    open_positions  : int           # number of open position rows
    open_orders     : int           # number of QUEUED/ACCEPTED orders

    # Data freshness
    prices_live     : bool          # True if Redis prices were available
    computed_at     : datetime      # when this was computed

    model_config = ConfigDict(from_attributes=True)
