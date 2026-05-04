# =============================================================================
# app/services/orders/order_service.py
# Main Orchestrator — Order Management Service
#
# PURPOSE:
#   This is the SINGLE ENTRY POINT for the API layer.
#   The API routes (orders.py) call ONLY this file.
#   This file coordinates all the submodules in the correct sequence.
#
# SUBMODULES CALLED:
#   intake.py            → create initial order record
#   validation.py        → run all 10 pre-trade checks
#   state_transitions.py → manage valid state changes + audit log
#   persistence.py       → block margin, create triggers, query orders
#   cancellation_engine.py → cancel orders cleanly
#
# PLACE ORDER FLOW (complete annotated lifecycle):
#
#   1. [intake]       Check for duplicate client_order_id
#   2. [intake]       Resolve instrument_id from symbol
#   3. [intake]       Create Order row in status=CREATED
#   4. [transitions]  Transition CREATED → PENDING_VALIDATION
#   5. [validation]   Run all 10 checks → get ValidationResult
#   6a.[transitions]  If FAIL → REJECTED (rejection_reason set)
#   6b.[transitions]  If PASS → ACCEPTED (margin_required set)
#   7. [persistence]  Block margin in wallet_ledgers
#   8. [persistence]  Create SL/TP OrderTrigger rows if sl/tp price given
#   9. [transitions]  Transition ACCEPTED → next_status:
#                       MARKET orders  → FILLED (immediate, simulated)
#                       LIMIT orders   → QUEUED (wait for LTP)
#                       SL/TP orders   → PENDING_TRIGGER (wait for trigger price)
#  10. [db.commit]    All changes committed atomically
# =============================================================================

import logging
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.order import Order, OrderStatus, OrderType
from app.models.user import User
from app.schemas.order import (
    OrderPlaceRequest, OrderCancelRequest,
    OrderFilterParams, MarginPreviewResponse
)
from app.services.order import intake, validation, state_transitions, persistence
from app.services.order.cancellation_engine import cancel_order as _cancel_order

logger = logging.getLogger(__name__)


# =============================================================================
# PLACE ORDER — Main orchestrator
# =============================================================================

def place_order(
    db     : Session,
    user   : User,
    payload: OrderPlaceRequest,
) -> Order:
    """
    Orchestrate the complete order placement flow.
    Called by: POST /api/v1/orders

    All database operations run in a SINGLE transaction.
    If anything fails after order creation, the entire transaction rolls back.
    """

    # ── Step 1: Check for duplicate order (idempotency) ───────────────────────
    existing = intake.check_duplicate_order(db, user.id, payload.client_order_id)
    if existing:
        # Return existing order — API response will be identical to original
        return existing

    # ── Step 2: Resolve instrument_id from symbol ──────────────────────────────
    from app.models.instruments import Instrument
    instrument = (
        db.query(Instrument)
        .filter(Instrument.symbol == payload.symbol.upper())
        .first()
    )
    if not instrument:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instrument '{payload.symbol}' not found in the system. "
                   f"Check the symbol format e.g. NSE:RELIANCE or CRYPTO:BTCUSDT"
        )

    # ── Step 3: Create initial order row in CREATED status ────────────────────
    order = intake.create_order_record(
        db             = db,
        user_id        = user.id,
        instrument_id  = str(instrument.id),
        payload        = payload,
    )

    # ── Step 4: Transition to PENDING_VALIDATION ──────────────────────────────
    # The audit log now shows: None→CREATED then CREATED→PENDING_VALIDATION
    state_transitions.transition(
        db        = db,
        order     = order,
        to_status = OrderStatus.PENDING_VALIDATION,
        reason    = "Starting pre-trade validation checks",
        actor     = "validation_svc",
    )

    # ── Step 5: Run all 10 validation checks ──────────────────────────────────
    result = validation.run_all_validations(db, user, payload)

    # ── Step 6a: Validation FAILED → reject the order ─────────────────────────
    if not result.passed:
        state_transitions.reject_order(
            db     = db,
            order  = order,
            reason = result.rejection_reason or "Validation failed",
            actor  = "validation_svc",
        )
        db.commit()
        db.refresh(order)
        return order  # Caller checks order.status to know it was rejected

    # ── Step 6b: Validation PASSED ────────────────────────────────────────────
    # Determine where the order goes next based on its type:
    next_status = _determine_next_status(payload.order_type)

    # ── Step 7 + 8: Accept order — sets margin, writes event ─────────────────
    state_transitions.accept_order(
        db             = db,
        order          = order,
        margin_required= result.margin_required,
        next_status    = next_status,
        actor          = "validation_svc",
    )

    # ── Step 9: Block the margin in the wallet ────────────────────────────────
    # DEBIT entry in wallet_ledgers — per spec 5.10 immutable ledger rule
    persistence.block_margin(
        db      = db,
        user_id = user.id,
        order_id= order.id,
        amount  = result.margin_required,
        symbol  = order.symbol,
    )

    # ── Step 10: Create SL/TP triggers if provided ───────────────────────────
    persistence.create_sl_tp_triggers(db, order)

    # ── Step 11: If MARKET order — simulate immediate fill ────────────────────
    # In production, the Execution Engine handles fills.
    # Here we simulate a direct fill for market orders.
    if next_status == OrderStatus.FILLED:
        _simulate_market_fill(db, order, result.current_price)

    # ── Step 12: Commit EVERYTHING atomically ──────────────────────────────────
    db.commit()
    db.refresh(order)

    logger.info(
        "Order #%s placed successfully: symbol=%s type=%s status=%s",
        order.id, order.symbol, order.order_type.value, order.status.value
    )
    return order


def _determine_next_status(order_type: OrderType) -> OrderStatus:
    """
    Determines where an accepted order goes next based on its type.

    MARKET     → FILLED           (execute immediately at current price)
    LIMIT      → QUEUED           (wait for LTP to reach limit price)
    SL_MARKET  → PENDING_TRIGGER  (wait for trigger price to be hit)
    SL_LIMIT   → PENDING_TRIGGER  (wait for trigger price to be hit)
    TAKE_PROFIT→ PENDING_TRIGGER  (wait for profit target price)
    """
    routing = {
        OrderType.MARKET          : OrderStatus.FILLED,
        OrderType.LIMIT           : OrderStatus.QUEUED,
        OrderType.STOP_LOSS_MARKET: OrderStatus.PENDING_TRIGGER,
        OrderType.STOP_LOSS_LIMIT : OrderStatus.PENDING_TRIGGER,
        OrderType.TAKE_PROFIT     : OrderStatus.PENDING_TRIGGER,
    }
    return routing.get(order_type, OrderStatus.QUEUED)


def _simulate_market_fill(
    db          : Session,
    order       : Order,
    current_price: Decimal | None,
) -> None:
    """
    Simulate immediate fill for MARKET orders.
    Production: Execution Engine handles this via fill_recorder.

    Updates order: filled_qty = quantity, average_price = LTP, releases margin.
    """
    if not current_price:
        # No price available — move to FAILED
        state_transitions.transition(
            db        = db,
            order     = order,
            to_status = OrderStatus.FAILED,
            reason    = "Market order could not be filled — no live price available",
            actor     = "execution_engine",
        )
        persistence.release_margin(db, order.user_id, order, "Market fill failed")
        return

    # Simulate fill
    order.filled_qty    = order.quantity
    order.average_price = current_price

    # Release margin after fill (in production, net P&L settles margin)
    persistence.release_margin(db, order.user_id, order, "Market order filled")

    logger.info(
        "Market order #%s filled at price=%.4f qty=%s",
        order.id, float(current_price), order.quantity
    )


# =============================================================================
# CANCEL ORDER
# =============================================================================

def cancel_order(
    db      : Session,
    order_id: int,
    user    : User,
    payload : OrderCancelRequest | None = None,
) -> Order:
    """
    Delegate to the Cancellation Engine.
    Called by: POST /api/v1/orders/{id}/cancel
    """
    return _cancel_order(db, order_id, user, payload)


# =============================================================================
# GET ORDER BY ID
# =============================================================================

def get_order(
    db      : Session,
    order_id: int,
    user    : User,
) -> Order:
    """
    Retrieve a single order with events + triggers.
    Called by: GET /api/v1/orders/{id}
    """
    order = persistence.get_order_by_id(db, order_id, user)
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order #{order_id} not found"
        )
    return order


# =============================================================================
# LIST ORDERS
# =============================================================================

def list_orders(
    db    : Session,
    user  : User,
    params: OrderFilterParams,
) -> tuple[int, list[Order]]:
    """
    Paginated, filtered order list with scope enforcement.
    Called by: GET /api/v1/orders
    """
    return persistence.list_orders_query(db, user, params)


# =============================================================================
# MARGIN PREVIEW
# =============================================================================

def get_margin_preview(
    db           : Session,
    user         : User,
    symbol       : str,
    side         : str,
    order_type   : str,
    quantity     : Decimal,
    price        : Decimal | None = None,
) -> MarginPreviewResponse:
    """
    Preview margin required BEFORE placing an order.
    Called by: GET /api/v1/orders/margin-preview
    No database writes — read-only calculation.
    """
    from app.models.instruments import Instrument
    from app.schemas.order import MarginPreviewResponse, OrderSide, OrderType

    instrument = db.query(Instrument).filter(
        Instrument.symbol == symbol.upper()
    ).first()
    if not instrument:
        raise HTTPException(404, f"Instrument not found: {symbol}")

    current_price = validation._get_current_price(symbol.upper())
    effective_price = price or current_price or Decimal("0")

    margin_required = Decimal("0")
    if effective_price > 0:
        rate = validation.MARGIN_RATES.get(instrument.instrument_type, Decimal("1.00"))
        margin_required = (quantity * effective_price * rate).quantize(Decimal("0.01"))

    free_balance   = validation._get_free_balance(db, user.id)
    blocked_margin = validation._get_blocked_margin(db, user.id)
    available      = free_balance - blocked_margin
    sufficient     = available >= margin_required
    shortfall      = max(Decimal("0"), margin_required - available) if not sufficient else None

    return MarginPreviewResponse(
        symbol            = symbol.upper(),
        side              = side,
        order_type        = order_type,
        quantity          = quantity,
        estimated_price   = effective_price,
        margin_required   = margin_required,
        available_balance = available,
        sufficient        = sufficient,
        shortfall         = shortfall,
        message           = (
            f"✓ Sufficient margin. Available ₹{available:.2f} ≥ Required ₹{margin_required:.2f}"
            if sufficient else
            f"✗ Insufficient. Need ₹{margin_required:.2f}, have ₹{available:.2f}. "
            f"Short by ₹{shortfall:.2f}"
        )
    )
