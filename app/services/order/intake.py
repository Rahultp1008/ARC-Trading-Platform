# =============================================================================
# app/services/orders/intake.py
# Submodule: Order Intake
#
# PURPOSE:
#   The intake submodule is the ENTRY POINT for every new order.
#   It is the first thing called when POST /orders is hit.
#
# RESPONSIBILITIES:
#   1. Check for duplicate orders using client_order_id (idempotency)
#   2. Create the initial Order row in status=CREATED
#   3. Write the first OrderEvent: None → CREATED
#   4. Hand control to the Validation Service
#
# WHY SEPARATION?
#   Separating intake from validation means the order always exists in DB
#   before validation runs. If the server crashes mid-validation, the order
#   row survives with status=CREATED and can be reconciled by a worker.
# =============================================================================

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.order import Order, OrderEvent, OrderStatus
from app.schemas.order import OrderPlaceRequest

logger = logging.getLogger(__name__)


def check_duplicate_order(
    db: Session,
    user_id: int,
    client_order_id: str | None
) -> Order | None:
    """
    If the client sends a client_order_id that already exists for this user,
    return the existing order instead of creating a new one.

    This makes the POST /orders endpoint idempotent — safe for retries.
    """
    if not client_order_id:
        return None

    existing = (
        db.query(Order)
        .filter(
            Order.user_id == user_id,
            Order.client_order_id == client_order_id
        )
        .first()
    )

    if existing:
        logger.info(
            "Duplicate client_order_id=%s for user_id=%s — returning existing order_id=%s",
            client_order_id, user_id, existing.id
        )
    return existing


def create_order_record(
    db: Session,
    user_id: int,
    instrument_id: str,
    payload: OrderPlaceRequest,
) -> Order:
    """
    Step 1 of the order lifecycle:
        Create the Order row in CREATED status.

    The row is written to DB immediately so the order exists even if
    subsequent validation fails. The event log captures every state change.

    Args:
        db           : Active SQLAlchemy session (no commit yet — caller commits)
        user_id      : The authenticated user placing the order
        instrument_id: UUID string from the instruments table
        payload      : Validated Pydantic request body

    Returns:
        Order object in status=CREATED (not yet committed)
    """
    now = datetime.now(timezone.utc)

    # ── Create the order row ──────────────────────────────────────────────────
    order = Order(
        user_id         = user_id,
        instrument_id   = instrument_id,
        symbol          = payload.symbol.upper(),
        order_type      = payload.order_type,
        side            = payload.side,
        product_type    = payload.product_type,
        quantity        = payload.quantity,
        filled_qty      = Decimal("0"),
        price           = payload.price,
        trigger_price   = payload.trigger_price,
        sl_price        = payload.sl_price,
        tp_price        = payload.tp_price,
        status          = OrderStatus.CREATED,
        client_order_id = payload.client_order_id,
        margin_required = Decimal("0"),    # Calculated later in validation
        margin_blocked  = Decimal("0"),    # Blocked later after acceptance
        placed_at       = now,
        updated_at      = now,
    )
    db.add(order)
    db.flush()  # Get order.id without committing the transaction

    # ── Write the first event: None → CREATED ────────────────────────────────
    # 'from_status=None' means this is the very first event in the order's life.
    first_event = OrderEvent(
        order_id    = order.id,
        from_status = None,
        to_status   = OrderStatus.CREATED.value,
        reason      = f"Order received via API | type={payload.order_type.value} | side={payload.side.value}",
        actor       = "user",
        actor_id    = user_id,
    )
    db.add(first_event)

    logger.info(
        "Order created: id=%s user=%s symbol=%s type=%s side=%s qty=%s",
        order.id, user_id, payload.symbol,
        payload.order_type.value, payload.side.value, payload.quantity
    )

    return order
