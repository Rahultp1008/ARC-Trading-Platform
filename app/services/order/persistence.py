# =============================================================================
# app/services/orders/persistence.py
# Submodule: Order Persistence & Query Service
#
# PURPOSE:
#   All database reads for orders go through this module.
#   All wallet/ledger writes for margin blocking go through here too.
#
# RESPONSIBILITIES:
#   1. Block margin in wallet_ledgers when order is accepted
#   2. Release margin in wallet_ledgers when order is cancelled/filled
#   3. Create SL/TP trigger rows in order_triggers
#   4. Query orders with scope enforcement (users see own, brokers see their users)
#   5. Paginated order listing with all filters
# =============================================================================

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select, case as sa_case
from sqlalchemy.orm import Session, selectinload

from app.models.order import (
    Order, OrderEvent, OrderStatus, OrderTrigger, TriggerSide, OrderSide
)
from app.models.user import User, UserRole
from app.models.funding import WalletLedger, LedgerEntryType
from app.schemas.order import OrderFilterParams

logger = logging.getLogger(__name__)


# =============================================================================
# WALLET: Margin blocking and release
# =============================================================================

def block_margin(
    db            : Session,
    user_id       : int,
    order_id      : int,
    amount        : Decimal,
    symbol        : str,
) -> None:
    """
    Write an immutable DEBIT ledger entry to block margin.
    Per spec 5.10: "Every balance change must produce an immutable ledger entry."

    This is called after order moves to ACCEPTED status.
    The amount is deducted from free balance immediately.
    """
    # Get current running balance to snapshot it
    current = db.scalar(
        select(func.coalesce(
            func.sum(
                sa_case(
                    (WalletLedger.entry_type == LedgerEntryType.CREDIT,  WalletLedger.amount),
                    (WalletLedger.entry_type == LedgerEntryType.DEBIT,  -WalletLedger.amount),
                    else_=Decimal("0"),
                )
            ), Decimal("0")
        )).where(WalletLedger.user_id == user_id)
    )
    running = Decimal(str(current or 0)) - amount

    entry = WalletLedger(
        user_id        = user_id,
        entry_type     = LedgerEntryType.DEBIT,
        amount         = amount,
        currency       = "INR",
        running_balance= running,
        source_type    = "order_margin_block",
        source_id      = order_id,
        description    = f"Margin blocked for order #{order_id} | {symbol}",
    )
    db.add(entry)
    logger.info("Margin blocked: user=%s order=%s amount=%.2f", user_id, order_id, amount)


def release_margin(
    db      : Session,
    user_id : int,
    order   : Order,
    reason  : str = "Order closed",
) -> None:
    """
    Write an immutable CREDIT ledger entry to release blocked margin.
    Called when an order is CANCELLED, FILLED, REJECTED, or EXPIRED.

    Only releases if margin_blocked > 0 — avoids double release.
    """
    if order.margin_blocked <= 0:
        return

    current = db.scalar(
        select(func.coalesce(
            func.sum(
                sa_case(
                    (WalletLedger.entry_type == LedgerEntryType.CREDIT,  WalletLedger.amount),
                    (WalletLedger.entry_type == LedgerEntryType.DEBIT,  -WalletLedger.amount),
                    else_=Decimal("0"),
                )
            ), Decimal("0")
        )).where(WalletLedger.user_id == user_id)
    )
    running = Decimal(str(current or 0)) + order.margin_blocked

    entry = WalletLedger(
        user_id        = user_id,
        entry_type     = LedgerEntryType.CREDIT,
        amount         = order.margin_blocked,
        currency       = "INR",
        running_balance= running,
        source_type    = "order_margin_release",
        source_id      = order.id,
        description    = f"Margin released for order #{order.id} | {order.symbol} | {reason}",
    )
    db.add(entry)

    # Zero out the blocked amount on the order
    released = order.margin_blocked
    order.margin_blocked = Decimal("0")

    logger.info(
        "Margin released: user=%s order=%s amount=%.2f reason=%s",
        user_id, order.id, released, reason
    )


# =============================================================================
# TRIGGERS: SL/TP creation
# =============================================================================

def create_sl_tp_triggers(
    db   : Session,
    order: Order,
) -> list[OrderTrigger]:
    """
    If the order has sl_price or tp_price, create OrderTrigger rows.
    The SL/TP watcher (spec section 8.2) will monitor these and fire when hit.

    TRIGGER SIDE LOGIC:
        For BUY positions:
            SL fires BELOW (protect against price falling)
            TP fires ABOVE (take profit when price rises)
        For SELL positions (short):
            SL fires ABOVE (protect against price rising)
            TP fires BELOW (take profit when price falls)
    """
    triggers = []
    is_buy = order.side == OrderSide.BUY

    if order.sl_price:
        sl_trigger = OrderTrigger(
            order_id              = order.id,
            trigger_type          = "sl",
            trigger_side          = TriggerSide.BELOW if is_buy else TriggerSide.ABOVE,
            trigger_price         = order.sl_price,
            order_type_on_trigger = "market",
        )
        db.add(sl_trigger)
        triggers.append(sl_trigger)

    if order.tp_price:
        tp_trigger = OrderTrigger(
            order_id              = order.id,
            trigger_type          = "tp",
            trigger_side          = TriggerSide.ABOVE if is_buy else TriggerSide.BELOW,
            trigger_price         = order.tp_price,
            order_type_on_trigger = "market",
        )
        db.add(tp_trigger)
        triggers.append(tp_trigger)

    if triggers:
        logger.info(
            "Created %d trigger(s) for order #%s: %s",
            len(triggers), order.id,
            [(t.trigger_type, str(t.trigger_price)) for t in triggers]
        )

    return triggers


# =============================================================================
# QUERIES: Scoped order retrieval
# =============================================================================

def get_order_by_id(
    db      : Session,
    order_id: int,
    user    : User,
) -> Order | None:
    """
    Get a single order with events and triggers eagerly loaded.
    Enforces scope: users see own orders, brokers see their users' orders.
    """
    query = (
        db.query(Order)
        .options(
            selectinload(Order.events),
            selectinload(Order.triggers),
        )
        .filter(Order.id == order_id)
    )

    order = query.first()
    if not order:
        return None

    # Scope enforcement
    role = user.role if hasattr(user.role, "value") else user.role
    role_str = role.value if hasattr(role, "value") else str(role)

    if role_str.upper() == "USER":
        # Regular users can only see their own orders
        if order.user_id != user.id:
            return None  # Return None → caller raises 404 (no info leakage)

    elif role_str.upper() == "BROKER":
        # Brokers can see orders of their users only
        order_owner = db.query(User).filter(User.id == order.user_id).first()
        if not order_owner or order_owner.broker_id != user.id:
            return None

    # Super Admin sees all orders — no filter needed

    return order


def list_orders_query(
    db    : Session,
    user  : User,
    params: OrderFilterParams,
) -> tuple[int, list[Order]]:
    """
    List orders with full filtering and pagination.
    Applies scope enforcement based on the requesting user's role.

    Returns:
        (total_count, page_of_orders)
    """
    role_str = (
        user.role.value if hasattr(user.role, "value") else str(user.role)
    ).upper()

    query = db.query(Order)

    # ── Scope enforcement ─────────────────────────────────────────────────────
    if role_str == "USER":
        query = query.filter(Order.user_id == user.id)
    elif role_str == "BROKER":
        # Get IDs of all users belonging to this broker
        broker_user_ids = [
            uid for (uid,) in db.query(User.id).filter(User.broker_id == user.id).all()
        ]
        query = query.filter(Order.user_id.in_(broker_user_ids))
    # SUPER_ADMIN sees all — no filter

    # ── Optional filters ──────────────────────────────────────────────────────
    if params.status:
        query = query.filter(Order.status == params.status)
    if params.symbol:
        query = query.filter(Order.symbol.ilike(f"%{params.symbol.upper()}%"))
    if params.side:
        query = query.filter(Order.side == params.side)
    if params.order_type:
        query = query.filter(Order.order_type == params.order_type)
    if params.product_type:
        query = query.filter(Order.product_type == params.product_type)
    if params.from_date:
        query = query.filter(Order.placed_at >= params.from_date)
    if params.to_date:
        query = query.filter(Order.placed_at <= params.to_date)

    # ── Total count for pagination metadata ───────────────────────────────────
    total = query.count()

    # ── Apply pagination — newest orders first ────────────────────────────────
    orders = (
        query
        .order_by(Order.placed_at.desc())
        .offset((params.page - 1) * params.size)
        .limit(params.size)
        .all()
    )

    return total, orders
