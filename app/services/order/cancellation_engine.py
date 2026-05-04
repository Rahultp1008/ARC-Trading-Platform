# =============================================================================
# app/services/orders/cancellation_engine.py
# Submodule: Cancellation Engine
#
# PURPOSE:
#   Handles the complete cancellation lifecycle for an order.
#   Cancellation is more than just setting status=CANCELLED — it must also:
#     1. Verify the order can be cancelled (not already filled/rejected)
#     2. Release any blocked margin back to the user's wallet
#     3. Mark SL/TP triggers as inactive (prevent them from firing)
#     4. Write a cancellation event to the audit log
#     5. Return the updated order with a clean state
#
# CANCELLABLE STATES (per spec):
#   created, pending_validation, accepted, queued, pending_trigger
#
# NON-CANCELLABLE STATES (terminal — cannot be undone):
#   filled, rejected, cancelled, expired, failed
# =============================================================================

import logging
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.order import Order, OrderStatus, OrderTrigger
from app.models.user import User, UserRole
from app.schemas.order import OrderCancelRequest
from app.services.order.state_transitions import transition
from app.services.order.persistence import release_margin

logger = logging.getLogger(__name__)


# States that can be cancelled
CANCELLABLE_STATES = frozenset({
    OrderStatus.CREATED,
    OrderStatus.PENDING_VALIDATION,
    OrderStatus.ACCEPTED,
    OrderStatus.QUEUED,
    OrderStatus.PENDING_TRIGGER,
})


def cancel_order(
    db      : Session,
    order_id: int,
    user    : User,
    payload : OrderCancelRequest | None = None,
) -> Order:
    """
    Cancel a pending order.

    Steps:
      1. Load the order and verify ownership
      2. Check it's in a cancellable state
      3. Release blocked margin back to wallet
      4. Deactivate any SL/TP triggers attached to this order
      5. Transition order to CANCELLED
      6. Commit and return

    Args:
        db       : Active database session
        order_id : ID of the order to cancel
        user     : The authenticated user requesting cancellation
        payload  : Optional body with cancellation reason

    Returns:
        The cancelled Order object

    Raises:
        HTTPException 404 — order not found or access denied
        HTTPException 422 — order is not in a cancellable state
    """
    # ── Step 1: Load the order ────────────────────────────────────────────────
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order #{order_id} not found"
        )

    # ── Step 2: Scope check ───────────────────────────────────────────────────
    role_str = (
        user.role.value if hasattr(user.role, "value") else str(user.role)
    ).upper()

    if role_str == "USER":
        # Users can only cancel their own orders
        if order.user_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only cancel your own orders"
            )
    elif role_str == "BROKER":
        # Brokers can cancel orders of their managed users
        from app.models.user import User as UserModel
        order_owner = db.query(UserModel).filter(UserModel.id == order.user_id).first()
        if not order_owner or order_owner.broker_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied — this user does not belong to your broker account"
            )
    # SUPER_ADMIN can cancel any order

    # ── Step 3: Check order is in a cancellable state ─────────────────────────
    if order.status not in CANCELLABLE_STATES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Order #{order_id} cannot be cancelled. "
                f"Current status: {order.status.value}. "
                f"Only orders in these states can be cancelled: "
                f"{[s.value for s in CANCELLABLE_STATES]}"
            )
        )

    # ── Step 4: Release blocked margin ───────────────────────────────────────
    # This writes an immutable CREDIT entry to wallet_ledgers.
    # The user's free balance is restored immediately.
    release_margin(
        db      = db,
        user_id = order.user_id,
        order   = order,
        reason  = "Order cancelled"
    )

    # ── Step 5: Deactivate all SL/TP triggers ─────────────────────────────────
    # If we don't do this, the SL/TP watcher might still fire after cancellation.
    # We mark is_triggered=True so the watcher skips them.
    active_triggers = (
        db.query(OrderTrigger)
        .filter(
            OrderTrigger.order_id   == order_id,
            OrderTrigger.is_triggered == False  # noqa: E712
        )
        .all()
    )

    now = datetime.now(timezone.utc)
    for trigger in active_triggers:
        trigger.is_triggered = True
        trigger.triggered_at = now
        logger.info(
            "Deactivated %s trigger for cancelled order #%s (price=%.4f)",
            trigger.trigger_type, order_id, float(trigger.trigger_price)
        )

    # ── Step 6: Build cancellation reason ────────────────────────────────────
    cancel_reason = "Cancelled by user"
    if payload and payload.reason:
        cancel_reason = payload.reason

    # ── Step 7: Transition to CANCELLED ──────────────────────────────────────
    transition(
        db        = db,
        order     = order,
        to_status = OrderStatus.CANCELLED,
        reason    = cancel_reason,
        actor     = f"user:{user.id}" if role_str == "USER" else f"broker:{user.id}",
        actor_id  = user.id,
        metadata  = {"triggers_deactivated": len(active_triggers)},
    )

    # ── Step 8: Commit everything atomically ──────────────────────────────────
    # All of the above (margin release, trigger deactivation, status change)
    # happen in the SAME transaction — either all succeed or all rollback.
    db.commit()
    db.refresh(order)

    logger.info(
        "Order #%s cancelled: user=%s reason=%s margin_released=%.2f",
        order_id, user.id, cancel_reason, float(order.margin_required)
    )

    return order
