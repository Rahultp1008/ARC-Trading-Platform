# =============================================================================
# app/services/portfolio/pnl_engine.py
# Submodules: RealizedPnLEngine + UnrealizedPnLEngine
#
# PURPOSE:
#   Two separate engines for two types of P&L:
#
#   REALIZED PnL:
#       Money you have ACTUALLY made by closing (selling) positions.
#       This is settled cash — it's yours.
#       Source: trade_logs table (each sell trade records realized_pnl).
#       Formula: (sell_price - avg_cost) × qty_sold - brokerage
#
#   UNREALIZED PnL:
#       Money you WOULD make if you sold your open positions RIGHT NOW.
#       This is not cash yet — it fluctuates with market prices.
#       Source: position table (avg_cost) + Redis (live LTP).
#       Formula: (current_LTP - avg_cost) × net_qty
#
# ASSET TYPE AWARENESS:
#   The engine handles 3 asset types differently:
#
#   EQUITY (shares):
#     unrealized = (LTP - avg_cost) × net_qty
#     Both LTP and avg_cost are in ₹/share
#
#   F&O (futures & options):
#     FUTURES: Same as equity formula (LTP is futures price)
#     OPTIONS:
#       BUY SIDE: unrealized = (current_premium - paid_premium) × lot_size
#       SELL SIDE (writer): unrealized = (collected_premium - current_premium) × lot_size
#       The avg_cost for options = premium paid per unit
#
#   CRYPTO (paper trading):
#     unrealized = (LTP_USDT - avg_cost_USDT) × qty_crypto
#     Qty can be fractional: 0.001 BTC, 0.5 ETH etc.
#     Uses Decimal(18,8) for precision
# =============================================================================

from __future__ import annotations

import logging
from decimal import Decimal
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# How old a Redis price can be before we consider it stale
STALE_PRICE_SECONDS = 30


# =============================================================================
# HELPER: Fetch live price from Redis
# =============================================================================

def _get_live_price(symbol: str) -> tuple[Decimal | None, bool]:
    """
    Fetch the Last Traded Price from Redis.

    Returns:
        (price, is_fresh)
        price    = Decimal LTP or None if not available
        is_fresh = True if price was updated recently (within 30 seconds)
    """
    try:
        from app.services.marketdata.price_cache import get_ltp
        data = get_ltp(symbol)

        if not data or not data.get("ltp"):
            return None, False

        ltp = Decimal(str(data["ltp"]))

        # Check if price is fresh (recently updated by simulator/feed)
        ts_str = data.get("timestamp")
        is_fresh = True
        if ts_str:
            try:
                quote_time = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                age_secs   = (datetime.now(timezone.utc) - quote_time).total_seconds()
                is_fresh   = age_secs <= STALE_PRICE_SECONDS
            except Exception:
                is_fresh = True   # If timestamp parsing fails, assume fresh

        return ltp, is_fresh

    except Exception as exc:
        logger.warning("Redis price fetch failed for %s: %s", symbol, exc)
        return None, False


# =============================================================================
# UNREALIZED PnL ENGINE
# =============================================================================

class UnrealizedPnLEngine:
    """
    Calculates unrealized (open) profit/loss using live prices from Redis.

    FORMULA BREAKDOWN:
        For each open position:
            ltp = get_ltp(symbol) from Redis         ← live market price
            unrealized = (ltp - avg_cost) × net_qty  ← your paper profit/loss

        If Redis is down:
            Falls back to avg_cost (assumes no change)
            Reports prices_live = False in summary
    """

    def calculate_position_pnl(
        self,
        position : object,
    ) -> tuple[Decimal, Decimal | None, bool]:
        """
        Calculate unrealized PnL for a single position.

        Steps:
            1. Get current LTP from Redis
            2. Detect asset type from product_type
            3. Apply the correct formula for that asset type
            4. Return (unrealized_pnl, current_ltp, prices_live)

        Returns:
            unrealized_pnl : the P&L amount (positive = profit, negative = loss)
            current_ltp    : the price used for calculation (None if unavailable)
            prices_live    : True if Redis had a fresh price
        """
        if not position.is_open or position.net_qty == 0:
            return Decimal("0"), None, True

        # ── Step 1: Get live price from Redis ─────────────────────────────────
        # Redis key pattern: ltp:{symbol} e.g. ltp:NSE:RELIANCE
        ltp, is_fresh = _get_live_price(position.symbol)

        if ltp is None:
            # No price available → unrealized PnL = 0 (we don't know)
            logger.debug("No live price for %s — unrealized PnL = 0", position.symbol)
            return Decimal("0"), None, False

        # ── Step 2: Calculate based on asset type ────────────────────────────
        product = (position.product_type or "intraday").lower()

        if product in ("futures",):
            # F&O FUTURES: same formula as equity
            # LTP is the futures contract price
            unrealized_pnl = self._equity_formula(ltp, position.avg_cost, position.net_qty)

        elif product in ("options",):
            # F&O OPTIONS:
            #   avg_cost = premium paid per unit (positive for buyer)
            #   If buyer (net_qty > 0):  profit when premium rises
            #   If seller (net_qty < 0): profit when premium falls
            #   Options have lot sizes — but net_qty already accounts for this
            unrealized_pnl = self._equity_formula(ltp, position.avg_cost, position.net_qty)

        elif "crypto" in position.symbol.lower() or "usdt" in position.symbol.lower():
            # CRYPTO:
            #   avg_cost is in USDT, ltp is in USDT
            #   net_qty can be fractional (0.001 BTC, 0.5 ETH)
            #   Formula is identical but Decimal precision matters more
            unrealized_pnl = self._equity_formula(ltp, position.avg_cost, position.net_qty)

        else:
            # EQUITY (default): straightforward formula
            unrealized_pnl = self._equity_formula(ltp, position.avg_cost, position.net_qty)

        return unrealized_pnl, ltp, is_fresh

    def _equity_formula(
        self,
        current_price : Decimal,
        avg_cost      : Decimal,
        net_qty       : Decimal,
    ) -> Decimal:
        """
        Standard unrealized PnL formula used for equity, futures, and crypto.

        Formula:
            unrealized_pnl = (current_price - avg_cost) × net_qty

        Example (LONG position):
            current_price = ₹2,600
            avg_cost      = ₹2,400
            net_qty       = 10 shares
            unrealized    = (2600 - 2400) × 10 = ₹2,000 profit

        Example (SHORT position):
            current_price = ₹2,300
            avg_cost      = ₹2,400 (sold short at this price)
            net_qty       = -10 shares (negative = short)
            unrealized    = (2300 - 2400) × (-10) = (-100) × (-10) = ₹1,000 profit
            (Short sellers profit when price falls)
        """
        if avg_cost <= 0 or net_qty == 0:
            return Decimal("0")
        return (current_price - avg_cost) * net_qty

    def calculate_all_positions(
        self,
        db      : Session,
        user_id : int,
    ) -> tuple[Decimal, list[dict], bool]:
        """
        Calculate unrealized PnL for ALL open positions of a user.

        Returns:
            total_unrealized : sum of all position PnL
            enriched_data    : list of position dicts with LTP and PnL added
            prices_live      : False if any price was unavailable
        """
        from app.models.position import Position

        positions = db.execute(
            select(Position).where(
                Position.user_id == user_id,
                Position.is_open == True,   # noqa: E712
                Position.net_qty > 0,
            )
        ).scalars().all()

        total_unrealized = Decimal("0")
        enriched = []
        all_live = True

        for pos in positions:
            upnl, ltp, is_live = self.calculate_position_pnl(pos)
            total_unrealized += upnl
            if not is_live:
                all_live = False

            # PnL percentage: unrealized_pnl / invested_value × 100
            invested = (pos.avg_cost or Decimal("0")) * (pos.net_qty or Decimal("0"))
            pnl_pct  = (upnl / invested * 100) if invested > 0 else Decimal("0")

            enriched.append({
                "position"      : pos,
                "unrealized_pnl": upnl,
                "current_ltp"   : ltp,
                "market_value"  : (ltp * pos.net_qty) if ltp else None,
                "pnl_pct"       : round(pnl_pct, 4),
                "prices_live"   : is_live,
            })

        return total_unrealized, enriched, all_live


# =============================================================================
# REALIZED PnL ENGINE
# =============================================================================

class RealizedPnLEngine:
    """
    Queries the trade_logs table to compute realized (settled) PnL.

    Realized PnL comes from SELL trades.
    Each sell trade records:
        realized_pnl = (sell_price - avg_cost_at_trade) × qty - brokerage

    This engine aggregates those records in different ways:
        - Total all-time realized PnL
        - Today's realized PnL only
        - Breakdown by asset class
        - Per-symbol history
    """

    def get_total_realized(self, db: Session, user_id: int) -> Decimal:
        """Sum of all realized_pnl from trade_logs for this user."""
        from app.models.fill import TradeLog

        result = db.scalar(
            select(func.coalesce(func.sum(TradeLog.realized_pnl), Decimal("0")))
            .where(TradeLog.user_id == user_id)
        )
        return Decimal(str(result or 0))

    def get_realized_by_asset_class(
        self,
        db      : Session,
        user_id : int,
    ) -> dict[str, Decimal]:
        """
        Break down realized PnL by asset class.
        Determines asset class from the symbol prefix or product_type.
        """
        from app.models.fill import TradeLog

        trades = db.execute(
            select(TradeLog).where(TradeLog.user_id == user_id)
        ).scalars().all()

        breakdown = {
            "equity": Decimal("0"),
            "fno"   : Decimal("0"),
            "crypto": Decimal("0"),
        }

        for trade in trades:
            pnl = trade.realized_pnl or Decimal("0")
            pt  = (trade.product_type or "").lower()
            sym = (trade.symbol or "").upper()

            if "CRYPTO" in sym or "USDT" in sym or "BTC" in sym or "ETH" in sym:
                breakdown["crypto"] += pnl
            elif pt in ("futures", "options"):
                breakdown["fno"] += pnl
            else:
                breakdown["equity"] += pnl

        return breakdown

    def get_total_brokerage(self, db: Session, user_id: int) -> Decimal:
        """Total brokerage/fees paid across all trades."""
        from app.models.fill import TradeLog

        result = db.scalar(
            select(func.coalesce(func.sum(TradeLog.brokerage), Decimal("0")))
            .where(TradeLog.user_id == user_id)
        )
        return Decimal(str(result or 0))
