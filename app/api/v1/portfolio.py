# =============================================================================
# app/api/v1/portfolio.py
# Module 8 — Position & Portfolio API | ARC Trading Platform
#
# ENDPOINTS (all under /api/v1/portfolio/):
#   GET /summary    → portfolio dashboard (equity, margin, PnL summary)
#   GET /positions  → all open positions with live unrealized PnL
#   GET /holdings   → delivery holdings only
#   GET /trades     → paginated trade history
#   GET /pnl        → detailed PnL breakdown by asset class
#
# ALL ENDPOINTS:
#   - Require JWT Bearer token
#   - Scope enforced: users see own, brokers see their users, admin sees all
#   - Live prices fetched from Redis at request time
# =============================================================================

from typing import Optional
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User, UserRole
from app.schemas.portfolio import (
    PortfolioSummary, PositionResponse, HoldingResponse,
    TradeListResponse, PnLBreakdownResponse
)
from app.services.portfolio.portfolio_service import (
    get_portfolio_summary,
    get_positions,
    get_holdings,
    get_trades,
    get_pnl_breakdown,
)

router = APIRouter(prefix="/portfolio", tags=["Portfolio & PnL"])


def _resolve_user_id(
    db              : Session,
    requesting_user : User,
    target_user_id  : Optional[int],
) -> int:
    """
    Resolve which user's portfolio to show.
    - Regular user: always their own
    - Broker: any of their managed users (if target_user_id provided)
    - Super Admin: any user (if target_user_id provided)
    """
    role = requesting_user.role.value if hasattr(requesting_user.role, "value") else str(requesting_user.role)

    if role.upper() == "USER":
        return requesting_user.id   # Users always see own portfolio

    if target_user_id and role.upper() in ("BROKER", "SUPER_ADMIN"):
        # Validate broker owns the target user
        if role.upper() == "BROKER":
            from app.models.user import User as UserModel
            target = db.query(UserModel).filter(UserModel.id == target_user_id).first()
            if not target or target.broker_id != requesting_user.id:
                from fastapi import HTTPException
                raise HTTPException(403, "Access denied — user does not belong to your broker account")
        return target_user_id

    return requesting_user.id


# =============================================================================
# GET /portfolio/summary
# =============================================================================

@router.get(
    "/summary",
    response_model = PortfolioSummary,
    summary        = "Get complete portfolio summary",
    description    = """
Returns the top-level portfolio dashboard metrics computed in real-time.

**The 7 required outputs (per spec 5.8):**
- `cash_balance` — free wallet cash (from wallet_ledger)
- `margin_used` — blocked by open orders
- `free_margin` — cash available for new trades
- `position_value` — market value of open intraday positions (live via Redis)
- `holdings_value` — market value of delivery holdings
- `unrealized_pnl` — live profit/loss on open positions
- `realized_pnl` — cumulative profit from all closed trades
- `total_equity` — cash + positions + holdings + unrealized
- `day_pnl` — today's equity vs yesterday's snapshot

**Note:** If Redis is unavailable, unrealized PnL will show as 0 and `prices_live` will be False.
""",
)
def summary(
    user_id : Optional[int] = Query(None, description="Target user ID (Broker/Admin only)"),
    db      : Session       = Depends(get_db),
    user    : User          = Depends(get_current_user),
) -> PortfolioSummary:
    target_id = _resolve_user_id(db, user, user_id)
    return get_portfolio_summary(db, target_id)


# =============================================================================
# GET /portfolio/positions
# =============================================================================

@router.get(
    "/positions",
    response_model = list[PositionResponse],
    summary        = "Get all open positions with live unrealized PnL",
    description    = """
Returns all currently open positions with real-time unrealized PnL.

Prices are fetched live from Redis (`ltp:{symbol}`) at request time.
Each position shows:
- `unrealized_pnl` — (LTP - avg_cost) × net_qty
- `pnl_pct` — percentage return on invested capital
- `market_value` — current market value in ₹

Includes all product types: intraday, delivery, futures, options, crypto.
""",
)
def positions(
    user_id : Optional[int] = Query(None, description="Target user ID (Broker/Admin only)"),
    db      : Session       = Depends(get_db),
    user    : User          = Depends(get_current_user),
) -> list[PositionResponse]:
    target_id = _resolve_user_id(db, user, user_id)
    return get_positions(db, target_id)


# =============================================================================
# GET /portfolio/holdings
# =============================================================================

@router.get(
    "/holdings",
    response_model = list[HoldingResponse],
    summary        = "Get delivery holdings (overnight positions)",
    description    = """
Returns only delivery (CNC) holdings — positions held overnight.

Unlike `/positions` which shows all open positions including intraday,
`/holdings` shows only long-term delivery positions.

Shows:
- `quantity` — units held (supports crypto decimals like 0.001 BTC)
- `avg_cost` — weighted average purchase price
- `invested_value` — avg_cost × quantity (what you paid)
- `current_value` — live price × quantity
- `unrealized_pnl` — current_value - invested_value
""",
)
def holdings(
    user_id : Optional[int] = Query(None, description="Target user ID (Broker/Admin only)"),
    db      : Session       = Depends(get_db),
    user    : User          = Depends(get_current_user),
) -> list[HoldingResponse]:
    target_id = _resolve_user_id(db, user, user_id)
    return get_holdings(db, target_id)


# =============================================================================
# GET /portfolio/trades
# =============================================================================

@router.get(
    "/trades",
    response_model = TradeListResponse,
    summary        = "Get paginated trade history",
    description    = """
Returns the complete trade history from the `trade_logs` table.

Every fill creates a trade log entry. This shows:
- `trade_type` — buy or sell
- `price` — fill price
- `quantity` — units traded (supports crypto decimals)
- `realized_pnl` — profit/loss on SELL trades (0 for buys)
- `brokerage` — fee charged per trade
- `net_value` — trade_value ± brokerage

**Pagination:** use `page` and `size` query params.
""",
)
def trades(
    user_id : Optional[int] = Query(None,  description="Target user ID (Broker/Admin only)"),
    page    : int           = Query(1,     ge=1,       description="Page number"),
    size    : int           = Query(20,    ge=1, le=100, description="Trades per page"),
    db      : Session       = Depends(get_db),
    user    : User          = Depends(get_current_user),
) -> TradeListResponse:
    target_id = _resolve_user_id(db, user, user_id)
    return get_trades(db, target_id, page, size)


# =============================================================================
# GET /portfolio/pnl
# =============================================================================

@router.get(
    "/pnl",
    response_model = PnLBreakdownResponse,
    summary        = "Get detailed PnL breakdown by asset class",
    description    = """
Returns a comprehensive PnL breakdown separating:

**By type:**
- `total_unrealized_pnl` — live paper profit on open positions
- `total_realized_pnl` — settled profit from closed trades
- `day_pnl` — today's P&L only

**By asset class (asset-type aware per spec 5.8):**
- Equity: NSE shares, ETFs, delivery holdings
- F&O: futures + options contracts
- Crypto: paper trading positions

**Brokerage:**
- `total_brokerage` — total fees paid across all trades
""",
)
def pnl(
    user_id : Optional[int] = Query(None, description="Target user ID (Broker/Admin only)"),
    db      : Session       = Depends(get_db),
    user    : User          = Depends(get_current_user),
) -> PnLBreakdownResponse:
    target_id = _resolve_user_id(db, user, user_id)
    return get_pnl_breakdown(db, target_id)
