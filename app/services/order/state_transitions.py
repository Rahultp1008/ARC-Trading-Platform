# =============================================================================
# app/services/orders/state_transitions.py
# Submodule: Order State Machine
#
# PURPOSE:
#   Every status change in an order MUST go through this module.
#   It ensures:
#     - Only valid transitions are allowed (no jumping from FILLED → QUEUED)
#     - Every transition writes an immutable OrderEvent to the audit log
#     - Timestamps (accepted_at, filled_at, cancelled_at) are set correctly
#
# VALID TRANSITIONS MAP:
#   CREATED           → PENDING_VALIDATION
#   PENDING_VALIDATION→ REJECTED, ACCEPTED
#   ACCEPTED          → QUEUED, PENDING_TRIGGER, FILLED (market orders)
#   QUEUED            → PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED, FAILED
#   PENDING_TRIGGER   → ACCEPTED, CANCELLED, EXPIRED
#   PARTIALLY_FILLED  → FILLED, CANCELLED, FAILED
#   (Terminal states: REJECTED, FILLED, CANCELLED, EXPIRED, FAILED have no outgoing transitions)
# =============================================================================

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.order import Order, OrderEvent, OrderStatus

logger = logging.getLogger(__name__)


# ── Valid state transitions ───────────────────────────────────────────────────
# This acts as the specification for the state machine.
# Any transition NOT in this map will raise an error.
VALID_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.CREATED           : {OrderStatus.PENDING_VALIDATION},
    OrderStatus.PENDING_VALIDATION: {OrderStatus.REJECTED, OrderStatus.ACCEPTED},
    OrderStatus.ACCEPTED          : {
        OrderStatus.QUEUED, OrderStatus.PENDING_TRIGGER,
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,  # market order direct fill
        OrderStatus.FAILED
    },
    OrderStatus.QUEUED            : {
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.FAILED
    },
    OrderStatus.PENDING_TRIGGER   : {
        OrderStatus.ACCEPTED,    # trigger fires → re-enter execution
        OrderStatus.CANCELLED, OrderStatus.EXPIRED
    },
    OrderStatus.PARTIALLY_FILLED  : {
        OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.FAILED
    },
    # Terminal states — no outgoing transitions
    OrderStatus.REJECTED          : set(),
    OrderStatus.FILLED            : set(),
    OrderStatus.CANCELLED         : set(),
    OrderStatus.EXPIRED           : set(),
    OrderStatus.FAILED            : set(),
}

# States that cannot receive any further actions
TERMINAL_STATES = {
    OrderStatus.REJECTED, OrderStatus.FILLED,
    OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.FAILED
}


def transition(
    db          : Session,
    order       : Order,
    to_status   : OrderStatus,
    reason      : str | None = None,
    actor       : str = "system",
    actor_id    : int | None = None,
    metadata    : dict | None = None,
) -> Order:
    """
    Perform a validated state transition on an order.

    This function:
      1. Validates the transition is allowed
      2. Updates order.status and relevant timestamps
      3. Writes an immutable OrderEvent to the audit log
      4. Does NOT commit — caller is responsible for db.commit()

    Args:
        db        : Active SQLAlchemy session
        order     : The Order object to transition
        to_status : Target status
        reason    : Human-readable reason for the transition
        actor     : Who caused this: 'user', 'validation_svc', 'execution_engine', 'system'
        actor_id  : user.id if actor is a human user
        metadata  : Additional context to store in the event log

    Returns:
        The updated Order object (not yet committed)

    Raises:
        HTTPException 422 if the transition is not valid
    """
    from_status = order.status
    now = datetime.now(timezone.utc)

    # ── Guard: Check transition is valid ──────────────────────────────────────
    allowed = VALID_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        logger.error(
            "Invalid order transition: order_id=%s %s → %s",
            order.id, from_status.value, to_status.value
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Cannot transition order from {from_status.value} to {to_status.value}. "
                f"Valid transitions from {from_status.value}: "
                f"{[s.value for s in allowed] or 'none (terminal state)'}"
            )
        )

    # ── Update order status ───────────────────────────────────────────────────
    order.status     = to_status
    order.updated_at = now

    # ── Set lifecycle timestamps ──────────────────────────────────────────────
    # These are set ONCE and never overwritten (nullable fields).
    if to_status == OrderStatus.ACCEPTED and order.accepted_at is None:
        order.accepted_at = now
        logger.info(
            "Order accepted: id=%s symbol=%s type=%s side=%s qty=%s margin=%.2f",
            order.id, order.symbol, order.order_type.value,
            order.side.value, order.quantity, float(order.margin_required)
        )

    elif to_status == OrderStatus.FILLED and order.filled_at is None:
        order.filled_at = now

    elif to_status in (OrderStatus.CANCELLED, OrderStatus.EXPIRED) and order.cancelled_at is None:
        order.cancelled_at = now

    # ── Write immutable event record ──────────────────────────────────────────
    event = OrderEvent(
        order_id    = order.id,
        from_status = from_status.value,
        to_status   = to_status.value,
        reason      = reason,
        actor       = actor,
        actor_id    = actor_id,
        metadata_json = json.dumps(metadata) if metadata else None,
    )
    db.add(event)

    logger.debug(
        "Order %s transitioned: %s → %s (actor=%s)",
        order.id, from_status.value, to_status.value, actor
    )

    return order


def reject_order(
    db     : Session,
    order  : Order,
    reason : str,
    actor  : str = "validation_svc",
) -> Order:
    """
    Convenience wrapper: reject an order with a clear reason.
    Sets order.rejection_reason and transitions to REJECTED.
    """
    order.rejection_reason = reason
    return transition(
        db, order,
        to_status = OrderStatus.REJECTED,
        reason    = reason,
        actor     = actor,
    )


def accept_order(
    db             : Session,
    order          : Order,
    margin_required: Decimal,
    next_status    : OrderStatus,  # QUEUED, PENDING_TRIGGER, or FILLED
    actor          : str = "validation_svc",
) -> Order:
    """
    Convenience wrapper: accept an order after all validations pass.

    Sets margin_required and margin_blocked, then transitions ACCEPTED → next_status.
    The two-step transition (PENDING_VALIDATION → ACCEPTED → next) ensures
    'accepted_at' timestamp is always set even for immediate market fills.
    """
    # First transition: PENDING_VALIDATION → ACCEPTED
    order.margin_required = margin_required
    order.margin_blocked  = margin_required

    transition(
        db, order,
        to_status = OrderStatus.ACCEPTED,
        reason    = f"All validations passed. Margin blocked: ₹{margin_required:.2f}",
        actor     = actor,
        metadata  = {"margin_required": str(margin_required)},
    )

    # Second transition: ACCEPTED → QUEUED / PENDING_TRIGGER / FILLED
    return transition(
        db, order,
        to_status = next_status,
        reason    = f"Order routed to {next_status.value}",
        actor     = actor,
    )
