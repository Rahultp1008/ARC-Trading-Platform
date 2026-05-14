# =============================================================================
# app/services/portfolio/portfolio_service.py
# Main Portfolio Service — orchestrates all submodules
#
# This is the SINGLE ENTRY POINT called by app/api/v1/portfolio.py
# All 5 API endpoints call functions in this file only.
#
# SUBMODULES USED:
#   position_builder.py → PositionBuilder (weighted avg cost)
#   pnl_engine.py       → UnrealizedPnLEngine + RealizedPnLEngine
#   holdings_service()  → filters positions to delivery-only
#   snapshot_service()  → reads yesterday's snapshot for day P&L
# =============================================================================

from __future__ import annotations

import logging
from datetime import datetime, timezone, date
from decimal import Decimal, ROUND_DOWN

from sqlalchemy import select, func, case as sa_case
from sqlalchemy.orm import Session

from app.services.portfolio.pnl_engine import UnrealizedPnLEngine, RealizedPnLEngine
from app.schemas.portfolio import (
    PortfolioSummary, PositionResponse, HoldingResponse,
    TradeResponse, TradeListResponse, PnLBreakdownResponse
)

logger = logging.getLogger(__name__)

# Instantiate engines once (they are stateless)
unrealized_engine = UnrealizedPnLEngine()
realized_engine   = RealizedPnLEngine()


# =============================================================================
# WALLET HELPERS
# =============================================================================

def _get_wallet_balance(db: Session, user_id: int) -> Decimal:
    """
    Compute cash balance from wallet_ledgers.
    Formula: SUM(credits) - SUM(debits) for this user.
    """
    from app.models.funding import WalletLedger, LedgerEntryType

    result = db.scalar(
        select(
            func.coalesce(
                func.sum(
                    sa_case(
                        (WalletLedger.entry_type == LedgerEntryType.CREDIT,  WalletLedger.amount),
                        (WalletLedger.entry_type == LedgerEntryType.DEBIT,  -WalletLedger.amount),
                        else_=Decimal("0"),
                    )
                ), Decimal("0")
            )
        ).where(WalletLedger.user_id == user_id)
    )
    return Decimal(str(result or 0))


def _get_margin_used(db: Session, user_id: int) -> Decimal:
    """Total margin currently blocked by open orders."""
    from app.models.order import Order, OrderStatus

    result = db.scalar(
        select(func.coalesce(func.sum(Order.margin_blocked), Decimal("0")))
        .where(
            Order.user_id == user_id,
            Order.status.in_([
                OrderStatus.ACCEPTED, OrderStatus.QUEUED,
                OrderStatus.PENDING_TRIGGER, OrderStatus.PARTIALLY_FILLED
            ])
        )
    )
    return Decimal(str(result or 0))


def _get_open_order_count(db: Session, user_id: int) -> int:
    """Count of orders that are currently open (not terminal)."""
    from app.models.order import Order, OrderStatus
    return db.scalar(
        select(func.count(Order.id)).where(
            Order.user_id == user_id,
            Order.status.in_([
                OrderStatus.ACCEPTED, OrderStatus.QUEUED,
                OrderStatus.PENDING_TRIGGER, OrderStatus.PARTIALLY_FILLED
            ])
        )
    ) or 0


def _get_yesterday_equity(db: Session, user_id: int) -> Decimal:
    """Get yesterday's portfolio equity from snapshot for day P&L calculation."""
    from app.models.portfolio import PortfolioSnapshot

    yesterday = date.today()
    snap = db.execute(
        select(PortfolioSnapshot).where(
            PortfolioSnapshot.user_id      == user_id,
            PortfolioSnapshot.snapshot_date <  yesterday,
        ).order_by(PortfolioSnapshot.snapshot_date.desc()).limit(1)
    ).scalars().first()

    return snap.total_equity if snap else Decimal("0")


# =============================================================================
# 1. GET /portfolio/summary
# =============================================================================

def get_portfolio_summary(db: Session, user_id: int) -> PortfolioSummary:
    """
    Build the complete portfolio summary dashboard.

    CALCULATION SEQUENCE:
        Step 1: Get cash balance from wallet_ledger
        Step 2: Get margin blocked from open orders
        Step 3: Get all open positions → compute unrealized PnL via Redis
        Step 4: Get delivery holdings → compute holdings market value
        Step 5: Get cumulative realized PnL from trade_logs
        Step 6: Compute total_equity = cash + positions + holdings + unrealized
        Step 7: Compute day_pnl = today_equity - yesterday_snapshot

    All steps run in one DB session — no commits.
    """
    now = datetime.now(timezone.utc)

    # ── Step 1: Wallet ────────────────────────────────────────────────────────
    cash_balance = _get_wallet_balance(db, user_id)
    margin_used  = _get_margin_used(db, user_id)
    free_margin  = cash_balance - margin_used

    # ── Step 2: Unrealized PnL from live positions ────────────────────────────
    total_unrealized, enriched_positions, prices_live = \
        unrealized_engine.calculate_all_positions(db, user_id)

    # ── Step 3: Market value of open positions ────────────────────────────────
    position_value = Decimal("0")
    for item in enriched_positions:
        mv = item.get("market_value")
        if mv is not None:
            position_value += mv

    # ── Step 4: Holdings value (delivery positions only) ─────────────────────
    holdings_value = _get_holdings_market_value(db, user_id)

    # ── Step 5: Realized PnL ─────────────────────────────────────────────────
    realized_pnl = realized_engine.get_total_realized(db, user_id)

    # ── Step 6: Total equity ─────────────────────────────────────────────────
    # The complete portfolio value combining everything
    total_equity = cash_balance + position_value + holdings_value + total_unrealized

    # ── Step 7: Day PnL ──────────────────────────────────────────────────────
    yesterday_equity = _get_yesterday_equity(db, user_id)
    day_pnl = total_equity - yesterday_equity
    day_pnl_pct = (
        (day_pnl / yesterday_equity * 100)
        if yesterday_equity > 0 else Decimal("0")
    )

    # ── Step 8: Count open positions ─────────────────────────────────────────
    from app.models.position import Position
    open_count = db.scalar(
        select(func.count(Position.id)).where(
            Position.user_id == user_id,
            Position.is_open == True,  # noqa: E712
        )
    ) or 0

    return PortfolioSummary(
        cash_balance    = cash_balance.quantize(Decimal("0.01")),
        margin_used     = margin_used.quantize(Decimal("0.01")),
        free_margin     = free_margin.quantize(Decimal("0.01")),
        position_value  = position_value.quantize(Decimal("0.01")),
        holdings_value  = holdings_value.quantize(Decimal("0.01")),
        unrealized_pnl  = total_unrealized.quantize(Decimal("0.01")),
        realized_pnl    = realized_pnl.quantize(Decimal("0.01")),
        total_equity    = total_equity.quantize(Decimal("0.01")),
        day_pnl         = day_pnl.quantize(Decimal("0.01")),
        day_pnl_pct     = day_pnl_pct.quantize(Decimal("0.0001")),
        open_positions  = open_count,
        open_orders     = _get_open_order_count(db, user_id),
        prices_live     = prices_live,
        computed_at     = now,
    )


# =============================================================================
# 2. GET /portfolio/positions
# =============================================================================

def get_positions(db: Session, user_id: int) -> list[PositionResponse]:
    """
    Return all open positions with live unrealized PnL.
    Fetches live price from Redis for each position.
    """
    from app.models.position import Position

    positions = db.execute(
        select(Position).where(
            Position.user_id == user_id,
            Position.is_open == True,  # noqa: E712
        ).order_by(Position.last_trade_at.desc())
    ).scalars().all()

    result = []
    for pos in positions:
        upnl, ltp, _ = unrealized_engine.calculate_position_pnl(pos)
        invested      = (pos.avg_cost or Decimal("0")) * (pos.net_qty or Decimal("0"))
        pnl_pct       = (upnl / invested * 100) if invested > 0 else Decimal("0")

        result.append(PositionResponse(
            id              = pos.id,
            user_id         = pos.user_id,
            instrument_id   = pos.instrument_id,
            symbol          = pos.symbol,
            exchange        = pos.exchange,
            product_type    = pos.product_type,
            net_qty         = pos.net_qty,
            buy_qty         = pos.buy_qty,
            sell_qty        = pos.sell_qty or Decimal("0"),
            avg_cost        = pos.avg_cost,
            unrealized_pnl  = upnl.quantize(Decimal("0.01")),
            realized_pnl    = pos.realized_pnl or Decimal("0"),
            pnl_pct         = pnl_pct.quantize(Decimal("0.0001")),
            current_ltp     = ltp,
            market_value    = (ltp * pos.net_qty).quantize(Decimal("0.01")) if ltp else None,
            day_buy_qty     = pos.day_buy_qty or Decimal("0"),
            day_sell_qty    = pos.day_sell_qty or Decimal("0"),
            day_pnl         = pos.day_pnl or Decimal("0"),
            is_open         = pos.is_open,
            last_trade_at   = pos.last_trade_at,
        ))

    return result


# =============================================================================
# 3. GET /portfolio/holdings
# =============================================================================

def _get_holdings_market_value(db: Session, user_id: int) -> Decimal:
    """Compute total market value of all delivery holdings."""
    from app.models.position import Position

    delivery_positions = db.execute(
        select(Position).where(
            Position.user_id     == user_id,
            Position.product_type == "delivery",
            Position.is_open     == True,  # noqa: E712
            Position.net_qty     > 0,
        )
    ).scalars().all()

    total = Decimal("0")
    for pos in delivery_positions:
        ltp, _ = _get_ltp_simple(pos.symbol)
        if ltp:
            total += ltp * pos.net_qty
        else:
            total += pos.avg_cost * pos.net_qty  # fallback to cost value
    return total


def _get_ltp_simple(symbol: str) -> tuple[Decimal | None, bool]:
    """Simplified LTP fetch (no staleness check)."""
    try:
        from app.services.marketdata.price_cache import get_ltp
        data = get_ltp(symbol)
        if data and data.get("ltp"):
            return Decimal(str(data["ltp"])), True
    except Exception:
        pass
    return None, False


def get_holdings(db: Session, user_id: int) -> list[HoldingResponse]:
    """
    Return delivery holdings — positions held overnight.
    Filters to product_type='delivery' only.
    """
    from app.models.position import Position

    delivery_positions = db.execute(
        select(Position).where(
            Position.user_id     == user_id,
            Position.product_type == "delivery",
            Position.is_open     == True,  # noqa: E712
            Position.net_qty     > 0,
        ).order_by(Position.last_trade_at.desc())
    ).scalars().all()

    result = []
    for pos in delivery_positions:
        ltp, _ = _get_ltp_simple(pos.symbol)

        invested_value  = pos.avg_cost * pos.net_qty
        current_value   = (ltp * pos.net_qty) if ltp else None
        unrealized_pnl  = (current_value - invested_value) if current_value else Decimal("0")
        pnl_pct         = (unrealized_pnl / invested_value * 100) if invested_value > 0 else Decimal("0")

        # Detect instrument type from symbol
        sym = pos.symbol.upper()
        if "CRYPTO" in sym or "USDT" in sym or "BTC" in sym or "ETH" in sym:
            inst_type = "crypto"
        elif pos.product_type in ("futures", "options"):
            inst_type = pos.product_type
        else:
            inst_type = "equity"

        result.append(HoldingResponse(
            instrument_id   = pos.instrument_id,
            symbol          = pos.symbol,
            exchange        = pos.exchange,
            instrument_type = inst_type,
            quantity        = pos.net_qty,
            avg_cost        = pos.avg_cost,
            current_price   = ltp,
            invested_value  = invested_value.quantize(Decimal("0.01")),
            current_value   = current_value.quantize(Decimal("0.01")) if current_value else None,
            unrealized_pnl  = unrealized_pnl.quantize(Decimal("0.01")),
            pnl_pct         = pnl_pct.quantize(Decimal("0.0001")),
            day_change_pct  = None,   # Would need previous close — future enhancement
        ))

    return result


# =============================================================================
# 4. GET /portfolio/trades
# =============================================================================

def get_trades(
    db      : Session,
    user_id : int,
    page    : int = 1,
    size    : int = 20,
) -> TradeListResponse:
    """Return paginated trade history from trade_logs table."""
    from app.models.fill import TradeLog

    total = db.scalar(
        select(func.count(TradeLog.id)).where(TradeLog.user_id == user_id)
    ) or 0

    trades = db.execute(
        select(TradeLog).where(TradeLog.user_id == user_id)
        .order_by(TradeLog.traded_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).scalars().all()

    return TradeListResponse(
        total  = total,
        page   = page,
        size   = size,
        trades = [
            TradeResponse(
                id                = t.id,
                order_id          = t.order_id,
                symbol            = t.symbol,
                exchange          = t.exchange,
                trade_type        = t.trade_type.value if hasattr(t.trade_type, "value") else str(t.trade_type),
                product_type      = t.product_type,
                quantity          = t.quantity,
                price             = t.price,
                trade_value       = t.trade_value,
                brokerage         = t.brokerage,
                net_value         = t.net_value,
                realized_pnl      = t.realized_pnl or Decimal("0"),
                avg_cost_at_trade = t.avg_cost_at_trade,
                traded_at         = t.traded_at,
            )
            for t in trades
        ]
    )


# =============================================================================
# 5. GET /portfolio/pnl
# =============================================================================

def get_pnl_breakdown(db: Session, user_id: int) -> PnLBreakdownResponse:
    """
    Detailed PnL breakdown by asset class and type (realized vs unrealized).
    """
    # Unrealized PnL by asset class (from live positions)
    from app.models.position import Position

    open_positions = db.execute(
        select(Position).where(
            Position.user_id == user_id,
            Position.is_open == True,  # noqa: E712
        )
    ).scalars().all()

    unrealized = {"equity": Decimal("0"), "fno": Decimal("0"), "crypto": Decimal("0")}
    total_unrealized = Decimal("0")
    day_pnl = Decimal("0")

    for pos in open_positions:
        upnl, _, _ = unrealized_engine.calculate_position_pnl(pos)
        total_unrealized += upnl
        day_pnl          += pos.day_pnl or Decimal("0")

        sym = pos.symbol.upper()
        pt  = (pos.product_type or "").lower()
        if "CRYPTO" in sym or "USDT" in sym:
            unrealized["crypto"] += upnl
        elif pt in ("futures", "options"):
            unrealized["fno"] += upnl
        else:
            unrealized["equity"] += upnl

    # Realized PnL by asset class (from trade history)
    realized_breakdown = realized_engine.get_realized_by_asset_class(db, user_id)
    total_realized     = realized_engine.get_total_realized(db, user_id)
    total_brokerage    = realized_engine.get_total_brokerage(db, user_id)

    return PnLBreakdownResponse(
        total_unrealized_pnl = total_unrealized.quantize(Decimal("0.01")),
        total_realized_pnl   = total_realized.quantize(Decimal("0.01")),
        total_pnl            = (total_unrealized + total_realized).quantize(Decimal("0.01")),
        day_pnl              = day_pnl.quantize(Decimal("0.01")),
        equity_unrealized    = unrealized["equity"].quantize(Decimal("0.01")),
        equity_realized      = realized_breakdown["equity"].quantize(Decimal("0.01")),
        fno_unrealized       = unrealized["fno"].quantize(Decimal("0.01")),
        fno_realized         = realized_breakdown["fno"].quantize(Decimal("0.01")),
        crypto_unrealized    = unrealized["crypto"].quantize(Decimal("0.01")),
        crypto_realized      = realized_breakdown["crypto"].quantize(Decimal("0.01")),
        total_brokerage      = total_brokerage.quantize(Decimal("0.01")),
    )
