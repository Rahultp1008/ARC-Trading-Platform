# =============================================================================
# app/api/v1/risk.py
# Module 9 — Risk & Margin API | ARC Trading Platform
#
# ENDPOINTS:
#   GET /api/v1/risk/preview            — preview margin for a potential order
#   GET /api/v1/risk/health             — portfolio health ratios
#   GET /api/v1/risk/margin-requirement — margin needed for a trade (no wallet check)
#
# All endpoints require JWT authentication.
# Scope: Users see own, Brokers see their users, Admin sees all.
# =============================================================================

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.risk import (
    MarginPreviewResponse,
    MarginBreakdown,
    ValidationResult,
    HealthRatioResponse,
    PositionHealth,
    MarginRequirementResponse,
)
from app.services.risk.engine import (
    PreTradeMarginValidator,
    MarginCalculator,
    UserHealthMonitor,
    ProductRuleEngine,
)

router = APIRouter(prefix="/risk", tags=["Risk & Margin"])

# Instantiate service classes (stateless — safe to reuse)
_validator   = PreTradeMarginValidator()
_calc        = MarginCalculator()
_health_mon  = UserHealthMonitor()
_rules       = ProductRuleEngine()


def _resolve_user_id(
    db            : Session,
    requesting_user: User,
    target_user_id: Optional[int],
) -> int:
    """Resolve which user's risk data to return based on role."""
    role = (requesting_user.role.value
            if hasattr(requesting_user.role, "value")
            else str(requesting_user.role)).upper()

    if role == "USER":
        return requesting_user.id

    if target_user_id and role in ("BROKER", "SUPER_ADMIN"):
        if role == "BROKER":
            from app.models.user import User as UserModel
            target = db.query(UserModel).filter(
                UserModel.id == target_user_id
            ).first()
            if not target or target.broker_id != requesting_user.id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied — user does not belong to your broker account"
                )
        return target_user_id

    return requesting_user.id


# =============================================================================
# ENDPOINT 1: GET /risk/preview
# =============================================================================

@router.get(
    "/preview",
    response_model = MarginPreviewResponse,
    summary        = "Preview margin requirement for a potential order",
    description    = """
Runs all pre-trade risk checks and returns a complete analysis.

**What this shows:**
- Whether you can afford this trade (sufficient margin)
- Exactly how margin was calculated (step by step)
- Which validation checks passed and which failed
- How much margin you'll have left after the trade

**Validation checks run:**
1. Trading enabled for this symbol
2. Product type permissions (can you trade F&O?)
3. Order type vs price consistency
4. Quantity cap validation
5. Lot size compliance (F&O only)
6. Tick size compliance (limit orders only)
7. Live price availability (Redis)
8. Leverage limit by role
9. Portfolio exposure limit
10. Open positions count
11. Liquidation guardrail
12. **Margin sufficiency** — the main check

**Margin formula:**
```
effective_price = limit_price OR live_LTP from Redis
trade_value     = quantity × effective_price
margin_required = trade_value × margin_rate (20% intraday, 100% delivery)
sufficient      = available_margin >= margin_required
```
""",
)
def preview_margin(
    symbol      : str            = Query(..., description="e.g. NSE:RELIANCE"),
    order_type  : str            = Query(..., description="market, limit, stop_loss_market"),
    side        : str            = Query(..., description="buy or sell"),
    product_type: str            = Query(..., description="intraday, delivery, futures, options"),
    quantity    : Decimal        = Query(..., gt=0, description="Number of units"),
    price       : Optional[Decimal] = Query(None, description="Limit price — required for limit orders"),
    user_id     : Optional[int]  = Query(None, description="Target user ID (Broker/Admin only)"),
    db          : Session        = Depends(get_db),
    user        : User           = Depends(get_current_user),
) -> MarginPreviewResponse:

    target_id = _resolve_user_id(db, user, user_id)

    # If broker/admin is checking for another user, get that user object
    if target_id != user.id:
        from app.models.user import User as UserModel
        target_user = db.query(UserModel).filter(UserModel.id == target_id).first()
        if not target_user:
            raise HTTPException(status_code=404, detail=f"User {target_id} not found")
    else:
        target_user = user

    # Run all validations
    result = _validator.validate(
        db           = db,
        user         = target_user,
        symbol       = symbol.upper(),
        order_type   = order_type.lower(),
        side         = side.lower(),
        product_type = product_type.lower(),
        quantity     = quantity,
        price        = price,
    )

    # Build response
    bd = result["margin_breakdown"]
    margin_bd = MarginBreakdown(
        trade_value     = bd["trade_value"],
        margin_rate     = bd["margin_rate"],
        margin_rate_pct = bd["margin_rate_pct"],
        raw_margin      = bd["raw_margin"],
        minimum_margin  = bd["minimum_margin"],
        final_margin    = bd["final_margin"],
        leverage        = bd["leverage"],
    )

    val_list = [
        ValidationResult(
            check   = v["check"],
            passed  = v["passed"],
            message = v["message"],
        )
        for v in result["validations"]
    ]

    return MarginPreviewResponse(
        symbol           = result["symbol"],
        side             = result["side"],
        order_type       = result["order_type"],
        product_type     = result["product_type"],
        quantity         = result["quantity"],
        price            = result["price"],
        ltp              = result["ltp"],
        effective_price  = result["effective_price"],
        margin_breakdown = margin_bd,
        margin_required  = result["margin_required"],
        wallet_balance   = result["wallet_balance"],
        margin_used      = result["margin_used"],
        available_margin = result["available_margin"],
        margin_after     = result["margin_after"],
        sufficient       = result["sufficient"],
        can_place        = result["can_place"],
        validations      = val_list,
        rejection_reason = result["rejection_reason"],
        prices_live      = result["prices_live"],
        computed_at      = result["computed_at"],
    )


# =============================================================================
# ENDPOINT 2: GET /risk/health
# =============================================================================

@router.get(
    "/health",
    response_model = HealthRatioResponse,
    summary        = "Get portfolio health ratios and liquidation status",
    description    = """
Returns a complete portfolio health report showing the risk level of all open positions.

**Health ratio per position:**
```
health_ratio = (current_LTP × qty) / (avg_cost × qty) × 100

> 100% = HEALTHY  — position gained value
50-100% = WARNING  — approaching margin call
25-50%  = DANGER   — high risk zone
< 25%   = CRITICAL — liquidation risk
```

**Overall status:**
Aggregates all positions to give a single portfolio health score.

**Alerts generated when:**
- Any position drops below 50% health (WARNING)
- Any position drops below 25% health (DANGER — liquidation risk)
- Free margin drops below 25% of wallet balance
""",
)
def portfolio_health(
    user_id : Optional[int] = Query(None, description="Target user ID (Broker/Admin only)"),
    db      : Session       = Depends(get_db),
    user    : User          = Depends(get_current_user),
) -> HealthRatioResponse:

    target_id = _resolve_user_id(db, user, user_id)

    # Get wallet state
    wallet_balance, margin_used = _validator._get_wallet_state(db, target_id)

    # Compute health
    health = _health_mon.compute_health(db, target_id, wallet_balance, margin_used)

    # Build position health list
    pos_list = [
        PositionHealth(
            symbol         = p["symbol"],
            product_type   = p["product_type"],
            net_qty        = p["net_qty"],
            avg_cost       = p["avg_cost"],
            current_ltp    = p["current_ltp"],
            market_value   = p["market_value"],
            margin_required= p["margin_required"],
            unrealized_pnl = p["unrealized_pnl"],
            health_ratio   = p["health_ratio"],
            status         = p["status"],
        )
        for p in health["positions"]
    ]

    return HealthRatioResponse(
        wallet_balance       = health["wallet_balance"],
        total_margin_used    = health["total_margin_used"],
        free_margin          = health["free_margin"],
        total_position_value = health["total_position_value"],
        total_unrealized_pnl = health["total_unrealized_pnl"],
        overall_health_ratio = health["overall_health_ratio"],
        overall_status       = health["overall_status"],
        positions            = pos_list,
        margin_call_warning  = health["margin_call_warning"],
        liquidation_risk     = health["liquidation_risk"],
        alerts               = health["alerts"],
        open_positions       = health["open_positions"],
        warning_positions    = health["warning_positions"],
        danger_positions     = health["danger_positions"],
        computed_at          = datetime.now(timezone.utc),
    )


# =============================================================================
# ENDPOINT 3: GET /risk/margin-requirement
# =============================================================================

@router.get(
    "/margin-requirement",
    response_model = MarginRequirementResponse,
    summary        = "Get margin requirement for a trade (without wallet check)",
    description    = """
Calculates the margin required for a trade without checking if the user has enough funds.

Use this to show users how much margin they need **before** they have the funds.

**Margin calculation:**
```
trade_value     = quantity × effective_price
margin_required = trade_value × margin_rate
leverage        = 1 / margin_rate

Margin rates:
  intraday  → 20%  (5x leverage)
  delivery  → 100% (1x, no leverage)
  futures   → 15%  (~6.67x leverage)
  options   → 100% (full premium)
  crypto    → 100% (no leverage)
```

Also validates lot size and tick size compliance.
""",
)
def margin_requirement(
    symbol      : str            = Query(..., description="e.g. NSE:RELIANCE"),
    product_type: str            = Query(..., description="intraday, delivery, futures, options"),
    quantity    : Decimal        = Query(..., gt=0),
    price       : Optional[Decimal] = Query(None, description="Price to use — uses live LTP if not provided"),
    db          : Session        = Depends(get_db),
    user        : User           = Depends(get_current_user),
) -> MarginRequirementResponse:

    now = datetime.now(timezone.utc)
    sym = symbol.upper()
    pt  = product_type.lower()

    # Get live price
    ltp, prices_live = _validator._get_live_price(sym)
    effective_price  = price or ltp or Decimal("0")

    if effective_price <= 0:
        raise HTTPException(
            status_code=422,
            detail=f"No price available for {sym}. "
                   f"Start the simulator or provide a price parameter."
        )

    # Get instrument metadata
    instrument = _validator._get_instrument(db, sym)
    lot_size   = Decimal(str(instrument.lot_size  or 1))    if instrument else None
    tick_size  = Decimal(str(instrument.tick_size or 0.05)) if instrument else None

    # Validate lot size
    lot_ok, lot_msg = _rules.validate_lot_size(quantity, lot_size, pt)
    tick_ok, tick_msg = _rules.validate_tick_size(price, tick_size, "limit" if price else "market")

    # Calculate margin
    margin_info = _calc.calculate(quantity, effective_price, pt, sym)

    return MarginRequirementResponse(
        symbol            = sym,
        product_type      = pt,
        quantity          = quantity,
        effective_price   = effective_price,
        trade_value       = margin_info["trade_value"],
        margin_rate       = margin_info["margin_rate"],
        margin_rate_pct   = margin_info["margin_rate_pct"],
        margin_required   = margin_info["final_margin"],
        leverage          = margin_info["leverage"],
        minimum_margin    = margin_info["minimum_margin"],
        tick_size         = tick_size,
        lot_size          = lot_size,
        is_lot_compliant  = lot_ok,
        lot_compliance_msg= lot_msg,
        prices_live       = prices_live,
        computed_at       = now,
    )
