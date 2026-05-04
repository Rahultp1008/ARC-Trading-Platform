# =============================================================================
# app/api/v1/orders.py
# Module 6 — Order Management API Routes | ARC Trading Platform
#
# ENDPOINTS:
#   POST /api/v1/orders                   Place a new order
#   GET  /api/v1/orders/margin-preview    Preview margin before placing
#   GET  /api/v1/orders                   List/filter orders (paginated)
#   GET  /api/v1/orders/{id}              Get full order details
#   POST /api/v1/orders/{id}/cancel       Cancel a pending order
#
# NOTES FOR FRESHERS:
#   - Every endpoint requires a JWT Bearer token (Depends(get_current_user))
#   - The API layer does NO business logic — it only calls order_service.py
#   - Pydantic schemas validate input before the service layer is called
#   - HTTPException is raised by the service layer — not here
#   - response_model tells FastAPI how to serialize the output automatically
# =============================================================================

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.order import OrderSide, OrderStatus, OrderType, ProductType
from app.models.user import User
from app.schemas.order import (
    MarginPreviewResponse,
    OrderCancelRequest,
    OrderFilterParams,
    OrderListResponse,
    OrderPlaceRequest,
    OrderResponse,
    OrderSummaryResponse,
)
from app.services.order.order_service import (
    cancel_order,
    get_margin_preview,
    get_order,
    list_orders,
    place_order,
)

# ── Router setup ──────────────────────────────────────────────────────────────
# All routes are grouped under the "Order Management" tag in Swagger UI.
router = APIRouter(prefix="/orders", tags=["Order Management"])


# =============================================================================
# ROUTE 1: Place a new order
# POST /api/v1/orders
# =============================================================================

@router.post(
    "",
    response_model = OrderResponse,
    status_code    = status.HTTP_201_CREATED,
    summary        = "Place a new order",
    description    = """
Place a market, limit, stop-loss, or take-profit order.

**Order lifecycle after placing:**
1. Order created (`created` status)
2. Validation checks run (`pending_validation`)
3a. Any check fails → `rejected` with reason
3b. All checks pass → `accepted`
4. MARKET orders → immediately `filled` at current LTP
4. LIMIT orders  → moved to `queued` — waits for limit price
4. SL/TP orders  → moved to `pending_trigger` — waits for trigger price

**Required fields by order type:**
- `market` — only symbol, quantity, side, product_type
- `limit` — above + `price`
- `stop_loss_market` — above + `trigger_price`
- `stop_loss_limit` — above + `price` + `trigger_price`
- `take_profit` — above + `trigger_price`

**Validation checks performed:**
1. User account is active
2. KYC is approved
3. Product type permission (can_trade_equity/fno/crypto)
4. Instrument is active and trading_allowed
5. Market is open
6. Quantity > 0
7. F&O lot size multiple
8. Tick size compliance for limit price
9. Live price available (for market orders)
10. Sufficient margin in wallet
""",
)
def post_place_order(
    payload : OrderPlaceRequest,
    db      : Session = Depends(get_db),
    user    : User    = Depends(get_current_user),
) -> OrderResponse:
    """
    WHAT HAPPENS HERE:
        1. Pydantic validates payload (types, required fields, cross-field rules)
        2. order_service.place_order() runs the full lifecycle
        3. Returns the final order — check order.status to see outcome
           (REJECTED orders are still returned with status 201 but status=rejected)
    """
    order = place_order(db, user, payload)
    return OrderResponse.model_validate(order)


# =============================================================================
# ROUTE 2: Preview margin before placing
# GET /api/v1/orders/margin-preview
#
# IMPORTANT: This route MUST be defined BEFORE /{id} route.
# If it comes after, FastAPI will try to match "margin-preview" as an integer
# order_id and return a 422 validation error.
# =============================================================================

@router.get(
    "/margin-preview",
    response_model = MarginPreviewResponse,
    summary        = "Preview margin required before placing an order",
    description    = """
Calculate how much margin will be required for a potential order,
WITHOUT actually placing it. Use this to show the user whether
they have enough balance before they click the Place Order button.

**No order is created. No margin is blocked. Read-only.**
""",
)
def get_margin_preview_endpoint(
    symbol      : str           = Query(..., description="e.g. NSE:RELIANCE or CRYPTO:BTCUSDT"),
    side        : str           = Query(..., description="buy or sell"),
    order_type  : str           = Query(..., description="market, limit, stop_loss_market, stop_loss_limit, take_profit"),
    quantity    : Decimal       = Query(..., gt=0, description="Number of units"),
    price       : Optional[Decimal] = Query(None, gt=0, description="Limit price (optional — for limit orders)"),
    db          : Session       = Depends(get_db),
    user        : User          = Depends(get_current_user),
) -> MarginPreviewResponse:
    return get_margin_preview(db, user, symbol, side, order_type, quantity, price)


# =============================================================================
# ROUTE 3: List/filter orders
# GET /api/v1/orders
# =============================================================================

@router.get(
    "",
    response_model = OrderListResponse,
    summary        = "List orders — filter by status, symbol, date range",
    description    = """
Returns a paginated list of orders for the authenticated user.

**Scope rules:**
- Regular users: see only their own orders
- Brokers: see all orders for their managed users
- Super Admin: sees all orders platform-wide

**All filters are optional — combine any subset.**
""",
)
def get_orders_list(
    # Query param filters — all optional
    order_status : Optional[OrderStatus]  = Query(None, alias="status",       description="Filter by status"),
    symbol       : Optional[str]          = Query(None,                        description="Partial symbol search e.g. RELI"),
    side         : Optional[OrderSide]    = Query(None,                        description="buy or sell"),
    order_type   : Optional[OrderType]    = Query(None,                        description="market, limit etc."),
    product_type : Optional[ProductType]  = Query(None,                        description="intraday, delivery, futures, options"),
    page         : int                    = Query(default=1,   ge=1,           description="Page number"),
    size         : int                    = Query(default=20,  ge=1, le=100,   description="Orders per page"),
    db           : Session                = Depends(get_db),
    user         : User                   = Depends(get_current_user),
) -> OrderListResponse:
    # Build the filter params object
    params = OrderFilterParams(
        status       = order_status,
        symbol       = symbol,
        side         = side,
        order_type   = order_type,
        product_type = product_type,
        page         = page,
        size         = size,
    )

    total, orders = list_orders(db, user, params)

    return OrderListResponse(
        total  = total,
        page   = page,
        size   = size,
        orders = [OrderSummaryResponse.model_validate(o) for o in orders],
    )


# =============================================================================
# ROUTE 4: Get single order detail
# GET /api/v1/orders/{id}
# =============================================================================

@router.get(
    "/{order_id}",
    response_model = OrderResponse,
    summary        = "Get full order detail including audit trail and triggers",
    description    = """
Returns complete order information including:
- All order fields (price, quantity, status, timestamps)
- `events` — complete state transition audit trail (who changed what, when)
- `triggers` — attached SL/TP trigger instructions and their current state

Use the `events` array to debug why an order was rejected or to audit fills.
""",
)
def get_order_detail(
    order_id: int,
    db      : Session = Depends(get_db),
    user    : User    = Depends(get_current_user),
) -> OrderResponse:
    """
    Loads the order with events and triggers in a single optimized query
    (uses SQLAlchemy selectinload to avoid N+1 queries).
    """
    order = get_order(db, order_id, user)
    return OrderResponse.model_validate(order)


# =============================================================================
# ROUTE 5: Cancel an order
# POST /api/v1/orders/{id}/cancel
# =============================================================================

@router.post(
    "/{order_id}/cancel",
    response_model = OrderResponse,
    summary        = "Cancel a pending order",
    description    = """
Cancel an order that has not yet been filled.

**Cancellable states:** created, pending_validation, accepted, queued, pending_trigger

**What happens on cancellation:**
1. Order status → `cancelled`
2. Blocked margin is released back to wallet (immediate)
3. Any SL/TP triggers are deactivated (won't fire after cancellation)
4. Cancellation event written to audit trail

**Non-cancellable states:** filled, rejected, cancelled, expired, failed
These terminal states cannot be reversed.
""",
)
def post_cancel_order(
    order_id: int,
    payload : Optional[OrderCancelRequest] = None,
    db      : Session                      = Depends(get_db),
    user    : User                         = Depends(get_current_user),
) -> OrderResponse:
    """
    Delegates to the Cancellation Engine which handles:
    - Ownership verification
    - State validation
    - Margin release
    - Trigger deactivation
    - Atomic commit
    """
    order = cancel_order(db, order_id, user, payload)
    return OrderResponse.model_validate(order)
