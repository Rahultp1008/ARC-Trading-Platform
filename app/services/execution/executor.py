# =============================================================================
# app/services/execution/executor.py
# Module 7 — Execution Engine | ARC Trading Platform
#
# PURPOSE:
#   The Execution Engine is the CORE of the trading platform.
#   It converts accepted orders into fills and updates all downstream data.
#
# SUBMODULES IN THIS FILE:
#   1. QuoteFetcher        → Reads live price from Redis, checks staleness
#   2. MarketOrderExecutor → Fills market orders immediately
#   3. LimitOrderEvaluator → Checks if LTP meets limit price criteria
#   4. FillRecorder        → Writes fill to DB atomically
#   5. PositionUpdater     → Updates user position inside the same transaction
#   6. WebSocketEmitter    → Pushes order status to connected clients
#
# THE ATOMIC TRANSACTION (most important concept):
#   ALL of these happen in ONE database transaction:
#     Step 1: Lock the order row (SELECT FOR UPDATE)
#     Step 2: Check optimistic lock version
#     Step 3: Create OrderFill record
#     Step 4: Create TradeLog record
#     Step 5: Update Position (avg_cost, net_qty, pnl)
#     Step 6: Update Order status to FILLED
#     Step 7: Commit everything
#   If ANY step fails → ROLLBACK everything → no partial state
# =============================================================================

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models.order import Order, OrderStatus, OrderSide, OrderType
from app.models.fill import OrderFill, TradeLog, ExecutionEvent, FillType, TradeType, ExecutionEventType
from app.models.position import Position, PositionSide
from app.models.instruments import Instrument

logger = logging.getLogger(__name__)

# How old a price quote can be before we reject the order
# Per spec section 5.5: "Execution should reject stale quotes"
QUOTE_STALE_THRESHOLD_SECONDS = 10

# Brokerage: flat ₹20 per order or 0.03% of trade value (whichever is lower)
BROKERAGE_FLAT = Decimal("20.00")
BROKERAGE_PCT  = Decimal("0.0003")


# =============================================================================
# SUBMODULE 1 — QUOTE FETCHER
# =============================================================================

class QuoteResult:
    """Result of fetching a live quote from Redis."""
    def __init__(self):
        self.success   : bool           = False
        self.ltp       : Decimal | None = None
        self.age_secs  : float          = 0.0
        self.error     : str | None     = None


def fetch_live_quote(symbol: str) -> QuoteResult:
    """
    Fetch the latest LTP from Redis price cache.
    Checks if the quote is fresh enough to use for execution.

    Redis key pattern: ltp:{symbol}
    e.g. ltp:NSE:RELIANCE → {"ltp": 2456.30, "timestamp": "2026-04-27T..."}

    Per spec section 5.5:
        "All prices should include timestamp and source."
        "Execution should reject stale quotes beyond configured threshold."
    """
    result = QuoteResult()

    try:
        from app.services.marketdata.price_cache import get_ltp
        ltp_data = get_ltp(symbol)

        if not ltp_data:
            result.error = f"No live price in cache for {symbol}. Start the simulator."
            return result

        ltp = ltp_data.get("ltp")
        if not ltp:
            result.error = f"Price data malformed for {symbol}: {ltp_data}"
            return result

        # ── Check quote freshness ─────────────────────────────────────────────
        # The timestamp tells us when this price was last updated.
        # If it's too old, the price is stale and we should not trade on it.
        ts_str = ltp_data.get("timestamp")
        if ts_str:
            try:
                quote_time = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                now        = datetime.now(timezone.utc)
                age_secs   = (now - quote_time).total_seconds()
                result.age_secs = age_secs

                if age_secs > QUOTE_STALE_THRESHOLD_SECONDS:
                    result.error = (
                        f"Quote for {symbol} is stale ({age_secs:.1f}s old). "
                        f"Max allowed: {QUOTE_STALE_THRESHOLD_SECONDS}s. "
                        f"Is the simulator running?"
                    )
                    return result
            except Exception:
                # If timestamp parsing fails, proceed with no age check
                pass

        result.success = True
        result.ltp     = Decimal(str(ltp))
        return result

    except Exception as exc:
        result.error = f"Redis read error for {symbol}: {exc}"
        return result


# =============================================================================
# SUBMODULE 2 — BROKERAGE CALCULATOR
# =============================================================================

def calculate_brokerage(fill_value: Decimal) -> Decimal:
    """
    Calculate brokerage fee for a fill.
    Rule: ₹20 flat OR 0.03% of fill value — whichever is LOWER.
    """
    pct_fee = (fill_value * BROKERAGE_PCT).quantize(Decimal("0.01"))
    return min(BROKERAGE_FLAT, pct_fee)


# =============================================================================
# SUBMODULE 3 — OPTIMISTIC LOCK CHECK
# =============================================================================

def check_and_lock_order(
    db      : Session,
    order_id: int,
    expected_version: int,
) -> Order | None:
    """
    Atomically lock the order row and verify the version hasn't changed.

    HOW IT WORKS:
        SELECT ... WHERE id = order_id AND version = expected_version FOR UPDATE
        If 0 rows returned → another worker already processed this → return None
        If 1 row returned  → we have the lock → proceed with fill

    This is the OPTIMISTIC LOCKING check.
    The FOR UPDATE clause prevents other DB connections from reading this row
    until our transaction commits or rolls back.

    Beginner note: This is like putting a "reserved" sign on a restaurant table.
    Other waiters (workers) see the sign and go to the next table.
    """
    result = db.execute(
        select(Order)
        .where(Order.id == order_id)
        .where(Order.version == expected_version)   # ← optimistic lock check
        .with_for_update()                           # ← pessimistic lock for duration of transaction
    )
    order = result.scalars().first()
    return order


# =============================================================================
# SUBMODULE 4 — FILL RECORDER (Atomic DB Transaction)
# =============================================================================

def record_fill_atomically(
    db              : Session,
    order           : Order,
    fill_price      : Decimal,
    fill_qty        : Decimal,
    fill_type       : FillType,
    idempotency_key : str,
) -> tuple[bool, str, OrderFill | None]:
    """
    THE MOST IMPORTANT FUNCTION IN THE EXECUTION ENGINE.

    Atomically executes ALL of these in ONE database transaction:
        1. Check idempotency key → skip if already exists
        2. Create OrderFill record
        3. Create TradeLog record
        4. Update Position (avg_cost / realized_pnl)
        5. Update Order status → FILLED or PARTIALLY_FILLED
        6. Increment Order version (optimistic lock)
        7. Create ExecutionEvent audit record
        8. COMMIT → everything is saved
        OR ROLLBACK → nothing was saved (if any step fails)

    Returns:
        (success: bool, message: str, fill: OrderFill | None)
    """
    now = datetime.now(timezone.utc)

    # ── IDEMPOTENCY CHECK ─────────────────────────────────────────────────────
    # Before creating anything, check if this idempotency_key already exists.
    # If it does → this fill already happened → return success without re-doing.
    existing_fill = db.execute(
        select(OrderFill).where(OrderFill.idempotency_key == idempotency_key)
    ).scalars().first()

    if existing_fill:
        logger.info(
            "Fill already exists for idempotency_key=%s order_id=%s — skipping",
            idempotency_key, order.id
        )
        _log_execution_event(
            db, order, ExecutionEventType.FILL_SKIPPED,
            fill_price, message=f"Duplicate fill skipped. Key={idempotency_key}"
        )
        db.commit()
        return True, "Already filled (idempotency)", existing_fill

    try:
        # ── STEP 1: Calculate fill values ─────────────────────────────────────
        fill_value = (fill_qty * fill_price).quantize(Decimal("0.01"))
        brokerage  = calculate_brokerage(fill_value)
        is_buy     = order.side == OrderSide.BUY

        # ── STEP 2: Create OrderFill record ───────────────────────────────────
        fill = OrderFill(
            order_id              = order.id,
            user_id               = order.user_id,
            symbol                = order.symbol,
            instrument_id         = order.instrument_id,
            fill_type             = fill_type,
            fill_qty              = fill_qty,
            fill_price            = fill_price,
            fill_value            = fill_value,
            brokerage             = brokerage,
            idempotency_key       = idempotency_key,
            order_version_at_fill = order.version,
            filled_at             = now,
        )
        db.add(fill)
        db.flush()  # Get fill.id without committing

        # ── STEP 3: Update Position ───────────────────────────────────────────
        # Find existing position or create new one
        position = db.execute(
            select(Position).where(
                Position.user_id       == order.user_id,
                Position.instrument_id == order.instrument_id,
                Position.product_type  == order.product_type.value,
            ).with_for_update()  # Lock position row too
        ).scalars().first()

        realized_pnl = Decimal("0")

        if position is None:
            # First trade on this instrument — create new position
            position = Position(
                user_id       = order.user_id,
                instrument_id = order.instrument_id,
                symbol        = order.symbol,
                exchange      = order.symbol.split(":")[0] if ":" in order.symbol else "NSE",
                product_type  = order.product_type.value,
                net_qty       = Decimal("0"),
                buy_qty       = Decimal("0"),
                sell_qty      = Decimal("0"),
                avg_cost      = Decimal("0"),
                realized_pnl  = Decimal("0"),
                is_open       = True,
                side          = PositionSide.LONG,
                first_buy_at  = now if is_buy else None,
            )
            db.add(position)
            db.flush()

        if is_buy:
            # BUY fill → increase qty, recalculate avg cost
            position.recalculate_avg_cost(fill_qty, fill_price)
            position.net_qty  += fill_qty
            position.buy_qty  += fill_qty
            position.day_buy_qty   += fill_qty
            position.day_buy_value += fill_value
            if position.first_buy_at is None:
                position.first_buy_at = now
        else:
            # SELL fill → decrease qty, compute realized PnL
            realized_pnl = position.apply_sell(fill_qty, fill_price, brokerage)
            position.net_qty  -= fill_qty
            position.sell_qty += fill_qty
            position.day_sell_qty  = getattr(position, "day_sell_qty", Decimal("0")) + fill_qty
            position.day_pnl      += realized_pnl

        position.total_brokerage += brokerage
        position.last_trade_at    = now
        position.is_open          = position.net_qty > 0
        position.side             = (
            PositionSide.LONG  if position.net_qty >= 0
            else PositionSide.SHORT
        )

        # ── STEP 4: Create TradeLog ───────────────────────────────────────────
        trade_log = TradeLog(
            fill_id           = fill.id,
            order_id          = order.id,
            user_id           = order.user_id,
            symbol            = order.symbol,
            instrument_id     = order.instrument_id,
            trade_type        = TradeType.BUY if is_buy else TradeType.SELL,
            product_type      = order.product_type.value,
            exchange          = order.symbol.split(":")[0] if ":" in order.symbol else "NSE",
            quantity          = fill_qty,
            price             = fill_price,
            trade_value       = fill_value,
            brokerage         = brokerage,
            net_value         = fill_value - brokerage if not is_buy else fill_value + brokerage,
            realized_pnl      = realized_pnl,
            avg_cost_at_trade = position.avg_cost,
            traded_at         = now,
        )
        db.add(trade_log)

        # ── STEP 5: Update Order status and version ───────────────────────────
        total_filled = order.filled_qty + fill_qty
        is_fully_filled = total_filled >= order.quantity

        order.filled_qty    = total_filled
        order.average_price = fill_price  # simplified — production uses weighted avg
        order.version      += 1           # ← INCREMENT VERSION (optimistic lock)
        order.updated_at    = now

        if is_fully_filled:
            order.status    = OrderStatus.FILLED
            order.filled_at = now
        else:
            order.status    = OrderStatus.PARTIALLY_FILLED

        # ── STEP 6: Release margin from wallet ───────────────────────────────
        if is_fully_filled and order.margin_blocked > 0:
            _release_margin(db, order, now)

        # ── STEP 7: Log execution event ───────────────────────────────────────
        _log_execution_event(
            db, order, ExecutionEventType.FILL_SUCCESS,
            fill_price,
            message=f"Filled {fill_qty} @ {fill_price} | PnL: {realized_pnl:.2f}",
            fill_id=fill.id,
            idempotency_key=idempotency_key,
        )

        # ── STEP 8: COMMIT — all or nothing ──────────────────────────────────
        # This is the moment where ALL of the above becomes permanent.
        # If the server crashes before this line → NOTHING was saved.
        # If the server crashes after this line → EVERYTHING was saved.
        # There is no in-between state — this is what "atomic" means.
        db.commit()

        logger.info(
            "Fill committed: order=%s fill=%s qty=%s price=%s status=%s",
            order.id, fill.id, fill_qty, fill_price, order.status.value
        )

        # ── STEP 9: Emit WebSocket event (after commit) ───────────────────────
        # Done AFTER commit so WebSocket clients get confirmed data.
        _emit_order_update(order, fill)

        return True, f"Filled {fill_qty} @ {fill_price}", fill

    except IntegrityError as exc:
        # Unique constraint violation on idempotency_key
        # Another worker inserted the same key simultaneously
        db.rollback()
        logger.warning(
            "IntegrityError on fill (likely race condition): order=%s key=%s: %s",
            order.id, idempotency_key, exc
        )
        return False, "Duplicate fill detected by DB constraint — skipped", None

    except Exception as exc:
        db.rollback()
        logger.exception("Unexpected error during fill: order=%s: %s", order.id, exc)
        _log_execution_event(
            db, order, ExecutionEventType.FILL_FAILED,
            None, message=str(exc)
        )
        db.commit()
        return False, f"Fill failed: {exc}", None


# =============================================================================
# SUBMODULE 5 — MARKET ORDER EXECUTOR
# =============================================================================

def execute_market_order(db: Session, order: Order) -> tuple[bool, str]:
    """
    Fill a MARKET order immediately at current LTP.

    Per spec section 5.7:
        "Market orders require fresh quote."
        "Fill market orders immediately."

    Steps:
        1. Fetch live price from Redis
        2. Check price is fresh (not stale)
        3. Lock the order row (optimistic lock)
        4. Record fill atomically
    """
    logger.info("MarketOrderExecutor: processing order #%s %s", order.id, order.symbol)

    # Step 1 — Fetch live price
    quote = fetch_live_quote(order.symbol)
    if not quote.success:
        _log_execution_event(
            db, order, ExecutionEventType.STALE_QUOTE, None,
            message=quote.error
        )
        db.commit()
        return False, quote.error

    # Step 2 — Build idempotency key
    # Format: fill_{order_id}_{sequence}
    # For market orders, sequence is always 1 (one fill per market order)
    idempotency_key = f"fill_{order.id}_1"

    # Step 3 — Lock and verify order version
    locked_order = check_and_lock_order(db, order.id, order.version)
    if not locked_order:
        msg = f"Version conflict on order #{order.id} — another worker already processed it"
        logger.warning(msg)
        _log_execution_event(db, order, ExecutionEventType.VERSION_CONFLICT, quote.ltp, message=msg)
        db.commit()
        return False, msg

    # Step 4 — Execute fill atomically
    success, message, fill = record_fill_atomically(
        db             = db,
        order          = locked_order,
        fill_price     = quote.ltp,
        fill_qty       = order.quantity,      # Market orders fill 100% at once
        fill_type      = FillType.MARKET_IMMEDIATE,
        idempotency_key= idempotency_key,
    )

    return success, message


# =============================================================================
# SUBMODULE 6 — LIMIT ORDER EVALUATOR
# =============================================================================

class LimitOrderEvaluator:
    """
    Evaluates whether a LIMIT order should be filled based on current LTP.

    FILL CONDITIONS per spec section 5.7:
        BUY  limit: fill when LTP <= limit_price
            (price dropped to or below what user wanted to pay)
        SELL limit: fill when LTP >= limit_price
            (price rose to or above what user wanted to receive)

    Used by the LimitOrderWatcher background worker.
    """

    def should_fill(self, order: Order, current_ltp: Decimal) -> tuple[bool, str]:
        """
        Check if current LTP meets the limit fill condition.

        Returns:
            (should_fill: bool, reason: str)
        """
        if not order.price:
            return False, "Order has no limit price set"

        limit_price = Decimal(str(order.price))

        if order.side == OrderSide.BUY:
            # BUY: fill when LTP falls TO or BELOW limit price
            if current_ltp <= limit_price:
                return True, f"BUY limit met: LTP {current_ltp} <= limit {limit_price}"
            else:
                return False, f"BUY limit not met: LTP {current_ltp} > limit {limit_price}"

        else:
            # SELL: fill when LTP rises TO or ABOVE limit price
            if current_ltp >= limit_price:
                return True, f"SELL limit met: LTP {current_ltp} >= limit {limit_price}"
            else:
                return False, f"SELL limit not met: LTP {current_ltp} < limit {limit_price}"

    def evaluate_and_fill(
        self,
        db   : Session,
        order: Order,
    ) -> tuple[bool, str]:
        """
        Full evaluation cycle:
        1. Fetch LTP
        2. Check if limit condition is met
        3. If yes → execute fill atomically
        """
        # Fetch live price
        quote = fetch_live_quote(order.symbol)
        if not quote.success:
            return False, quote.error

        # Check limit condition
        should, reason = self.should_fill(order, quote.ltp)

        if not should:
            # Price not at limit yet — log check and move on
            _log_execution_event(
                db, order, ExecutionEventType.LIMIT_CHECKED,
                quote.ltp, message=reason
            )
            db.commit()
            return False, reason

        # ── Limit condition MET — execute the fill ────────────────────────────
        logger.info(
            "Limit condition met for order #%s: %s — executing fill",
            order.id, reason
        )

        # Build idempotency key
        idempotency_key = f"fill_{order.id}_limit_1"

        # Lock order with optimistic lock check
        locked_order = check_and_lock_order(db, order.id, order.version)
        if not locked_order:
            msg = f"Version conflict on order #{order.id}"
            _log_execution_event(db, order, ExecutionEventType.VERSION_CONFLICT, quote.ltp, message=msg)
            db.commit()
            return False, msg

        # Execute fill atomically
        success, message, fill = record_fill_atomically(
            db              = db,
            order           = locked_order,
            fill_price      = quote.ltp,      # Fill at current LTP (not necessarily limit price)
            fill_qty        = order.quantity,
            fill_type       = FillType.LIMIT_TRIGGERED,
            idempotency_key = idempotency_key,
        )

        return success, message


# =============================================================================
# SUBMODULE 7 — SL/TP TRIGGER EVALUATOR
# =============================================================================

def evaluate_sl_tp_trigger(db: Session, order: Order) -> tuple[bool, str]:
    """
    Check if a PENDING_TRIGGER order should fire.
    Reads the OrderTrigger rows and checks if LTP crossed the trigger price.

    Per spec section 8.2:
        "Prevent duplicate trigger execution."
        "Handle trigger-side logic per long/short position."
        "Respect fresh quote requirement."
    """
    from app.models.order import OrderTrigger, TriggerSide

    # Fetch live price
    quote = fetch_live_quote(order.symbol)
    if not quote.success:
        return False, quote.error

    current_ltp = quote.ltp

    # Get all non-triggered triggers for this order
    triggers = db.execute(
        select(OrderTrigger).where(
            OrderTrigger.order_id    == order.id,
            OrderTrigger.is_triggered == False,
        )
    ).scalars().all()

    if not triggers:
        return False, f"No active triggers for order #{order.id}"

    fired = False
    now   = datetime.now(timezone.utc)

    for trigger in triggers:
        trigger_price = Decimal(str(trigger.trigger_price))
        should_fire   = False

        if trigger.trigger_side == TriggerSide.ABOVE:
            # Fire when LTP goes ABOVE trigger price
            should_fire = current_ltp >= trigger_price
        elif trigger.trigger_side == TriggerSide.BELOW:
            # Fire when LTP goes BELOW trigger price
            should_fire = current_ltp <= trigger_price

        if should_fire:
            logger.info(
                "Trigger fired: order=%s type=%s ltp=%s trigger=%s",
                order.id, trigger.trigger_type, current_ltp, trigger_price
            )

            # Mark trigger as fired (prevents double execution)
            trigger.is_triggered      = True
            trigger.triggered_at      = now
            trigger.triggered_at_price= current_ltp

            # Build idempotency key
            idempotency_key = f"fill_{order.id}_{trigger.trigger_type}_trigger"

            # Execute the fill
            fill_type = (
                FillType.SL_TRIGGERED if trigger.trigger_type == "sl"
                else FillType.TP_TRIGGERED
            )

            locked_order = check_and_lock_order(db, order.id, order.version)
            if not locked_order:
                db.rollback()
                return False, f"Version conflict on order #{order.id}"

            success, message, fill = record_fill_atomically(
                db              = db,
                order           = locked_order,
                fill_price      = current_ltp,
                fill_qty        = order.quantity,
                fill_type       = fill_type,
                idempotency_key = idempotency_key,
            )
            fired = True
            if not success:
                return False, message

    return fired, "Trigger evaluated"


# =============================================================================
# HELPERS — Internal use only
# =============================================================================

def _release_margin(db: Session, order: Order, now: datetime) -> None:
    """Release blocked margin back to wallet when order is fully filled."""
    from app.models.funding import WalletLedger, LedgerEntryType
    from sqlalchemy import select, func, case as sa_case

    if order.margin_blocked <= 0:
        return

    # Get current balance for running_balance snapshot
    result = db.scalar(
        select(
            func.coalesce(
                func.sum(sa_case(
                    (WalletLedger.entry_type == LedgerEntryType.CREDIT,  WalletLedger.amount),
                    (WalletLedger.entry_type == LedgerEntryType.DEBIT,  -WalletLedger.amount),
                    else_=Decimal("0"),
                )), Decimal("0")
            )
        ).where(WalletLedger.user_id == order.user_id)
    )
    running = Decimal(str(result or 0)) + order.margin_blocked

    entry = WalletLedger(
        user_id        = order.user_id,
        entry_type     = LedgerEntryType.CREDIT,
        amount         = order.margin_blocked,
        currency       = "INR",
        running_balance= running,
        source_type    = "order_margin_release",
        source_id      = order.id,
        description    = f"Margin released after fill of order #{order.id} | {order.symbol}",
    )
    db.add(entry)
    order.margin_blocked = Decimal("0")


def _log_execution_event(
    db             : Session,
    order          : Order,
    event_type     : ExecutionEventType,
    ltp            : Decimal | None,
    message        : str | None = None,
    fill_id        : int | None = None,
    idempotency_key: str | None = None,
    age_secs       : float = 0.0,
) -> None:
    """Write an ExecutionEvent audit record."""
    event = ExecutionEvent(
        order_id        = order.id,
        user_id         = order.user_id,
        event_type      = event_type,
        symbol          = order.symbol,
        ltp_at_event    = ltp,
        quote_age_secs  = age_secs,
        success         = event_type == ExecutionEventType.FILL_SUCCESS,
        message         = message,
        fill_id         = fill_id,
        idempotency_key = idempotency_key,
    )
    db.add(event)


def _emit_order_update(order: Order, fill: OrderFill) -> None:
    """
    WebSocket stub — emit order status update to all subscribed clients.

    Per spec section 5.7: "Emit order status events over WebSocket."

    In production this pushes to ws_manager which broadcasts to the
    user's connected browser/mobile app so the UI updates in real time.
    """
    try:
        import asyncio
        from app.services.marketdata.ws_manager import manager

        message = {
            "type"        : "order_update",
            "order_id"    : order.id,
            "user_id"     : order.user_id,
            "symbol"      : order.symbol,
            "status"      : order.status.value,
            "filled_qty"  : str(order.filled_qty),
            "average_price": str(order.average_price) if order.average_price else None,
            "fill_price"  : str(fill.fill_price),
            "fill_qty"    : str(fill.fill_qty),
            "timestamp"   : datetime.now(timezone.utc).isoformat(),
        }

        # Run async broadcast in sync context
        # In production this would be done via a message queue (Redis pub/sub)
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We are inside an async context — schedule the broadcast
            asyncio.ensure_future(manager.broadcast(str(message)))
        else:
            loop.run_until_complete(manager.broadcast(str(message)))

        logger.info(
            "WebSocket: emitted order_update for order #%s status=%s",
            order.id, order.status.value
        )

    except Exception as exc:
        # WebSocket emission failure must NEVER break the fill
        # The fill is already committed — WS is best-effort
        logger.warning("WebSocket emission failed (non-critical): %s", exc)
