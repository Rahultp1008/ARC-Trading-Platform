# =============================================================================
# app/workers/limit_watcher.py
# Module 7 — Execution Engine | ARC Trading Platform
#
# PURPOSE:
#   Background worker that continuously monitors QUEUED limit orders
#   and fills them when the current LTP meets their limit price condition.
#
# HOW IT WORKS:
#   1. Every N seconds, query DB for all orders in QUEUED status
#   2. For each order, fetch the current LTP from Redis
#   3. Check if LTP meets the limit condition (buy: LTP <= limit, sell: LTP >= limit)
#   4. If yes → execute fill atomically via the Execution Engine
#   5. Sleep and repeat
#
# ALSO HANDLES:
#   - SL/TP trigger orders (PENDING_TRIGGER status)
#   - Intraday auto square-off at 3:20 PM IST
#   - Stuck order detection (orders in PENDING_VALIDATION for too long)
#
# PER SPEC section 8.1:
#   "Monitors pending limit orders and executes when criteria are met."
# PER SPEC section 8.2:
#   "Prevent duplicate trigger execution."
# =============================================================================

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.order import Order, OrderStatus
from app.services.execution.executor import (
    LimitOrderEvaluator,
    evaluate_sl_tp_trigger,
)

logger = logging.getLogger(__name__)

# How often the watcher checks for fillable orders (in seconds)
# Per spec section 8.2 — check interval not specified, we use 1 second
POLL_INTERVAL_SECONDS = 1

# How many orders to process per batch (prevents overloading DB)
BATCH_SIZE = 50

# Intraday auto square-off time (3:20 PM IST = 9:50 AM UTC)
# In production use IST timezone properly
INTRADAY_SQUAREOFF_HOUR_UTC   = 9
INTRADAY_SQUAREOFF_MINUTE_UTC = 50


# =============================================================================
# LIMIT ORDER WATCHER CLASS
# =============================================================================

class LimitOrderWatcher:
    """
    Background async task that watches QUEUED orders and fills them.

    Lifecycle:
        start() → begins watching in background
        stop()  → gracefully stops the watcher

    Used in app/main.py lifespan:
        await limit_watcher.start()
    """

    def __init__(self):
        self._task    : asyncio.Task | None = None
        self._running : bool                = False
        self._evaluator = LimitOrderEvaluator()

    async def start(self):
        """Start the background watcher task."""
        if self._task and not self._task.done():
            logger.info("LimitOrderWatcher already running")
            return

        self._running = True
        self._task    = asyncio.create_task(self._watch_loop())
        logger.info(
            "LimitOrderWatcher started — polling every %ds", POLL_INTERVAL_SECONDS
        )

    async def stop(self):
        """Gracefully stop the watcher."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("LimitOrderWatcher stopped")

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    # ── Main poll loop ────────────────────────────────────────────────────────

    async def _watch_loop(self):
        """
        The main loop. Runs forever until stop() is called.
        Uses asyncio.sleep() so it does not block the event loop.
        """
        logger.info("LimitOrderWatcher: watch loop started")

        while self._running:
            try:
                # Process limit orders
                await asyncio.get_event_loop().run_in_executor(
                    None, self._process_queued_orders
                )

                # Process SL/TP trigger orders
                await asyncio.get_event_loop().run_in_executor(
                    None, self._process_trigger_orders
                )

                # Check for intraday square-off
                await asyncio.get_event_loop().run_in_executor(
                    None, self._check_intraday_squareoff
                )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                # Never crash the watcher — log and continue
                logger.exception("LimitOrderWatcher error (continuing): %s", exc)

            # Wait before next poll
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

        logger.info("LimitOrderWatcher: watch loop ended")

    # ── Process QUEUED limit orders ───────────────────────────────────────────

    def _process_queued_orders(self):
        """
        Query all QUEUED orders and evaluate each one for filling.
        Runs in a thread pool to avoid blocking the async event loop.
        """
        db = SessionLocal()
        try:
            # Fetch a batch of QUEUED orders
            # ORDER BY placed_at ensures FIFO — oldest orders filled first
            queued_orders = db.execute(
                select(Order)
                .where(Order.status == OrderStatus.QUEUED)
                .order_by(Order.placed_at.asc())
                .limit(BATCH_SIZE)
            ).scalars().all()

            if not queued_orders:
                return  # Nothing to process

            logger.debug(
                "LimitWatcher: evaluating %d queued orders", len(queued_orders)
            )

            filled_count  = 0
            skipped_count = 0

            for order in queued_orders:
                try:
                    success, message = self._evaluator.evaluate_and_fill(db, order)
                    if success:
                        filled_count += 1
                        logger.info(
                            "LimitWatcher: filled order #%s %s — %s",
                            order.id, order.symbol, message
                        )
                    else:
                        skipped_count += 1

                except Exception as exc:
                    # Error on one order must not stop processing others
                    logger.exception(
                        "LimitWatcher: error evaluating order #%s: %s",
                        order.id, exc
                    )
                    try:
                        db.rollback()
                    except Exception:
                        pass

            if filled_count > 0:
                logger.info(
                    "LimitWatcher: batch complete — filled=%d skipped=%d",
                    filled_count, skipped_count
                )

        except Exception as exc:
            logger.exception("LimitWatcher._process_queued_orders error: %s", exc)
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()

    # ── Process PENDING_TRIGGER orders ────────────────────────────────────────

    def _process_trigger_orders(self):
        """
        Query all PENDING_TRIGGER orders and evaluate SL/TP triggers.

        Per spec section 8.2:
            "Prevent duplicate trigger execution." — handled by idempotency_key
            "Respect fresh quote requirement." — handled in evaluate_sl_tp_trigger
        """
        db = SessionLocal()
        try:
            trigger_orders = db.execute(
                select(Order)
                .where(Order.status == OrderStatus.PENDING_TRIGGER)
                .order_by(Order.placed_at.asc())
                .limit(BATCH_SIZE)
            ).scalars().all()

            if not trigger_orders:
                return

            for order in trigger_orders:
                try:
                    success, message = evaluate_sl_tp_trigger(db, order)
                    if success:
                        logger.info(
                            "LimitWatcher: SL/TP trigger fired for order #%s: %s",
                            order.id, message
                        )
                except Exception as exc:
                    logger.exception(
                        "LimitWatcher: SL/TP error for order #%s: %s",
                        order.id, exc
                    )
                    try:
                        db.rollback()
                    except Exception:
                        pass

        except Exception as exc:
            logger.exception("LimitWatcher._process_trigger_orders error: %s", exc)
        finally:
            db.close()

    # ── Intraday auto square-off ──────────────────────────────────────────────

    def _check_intraday_squareoff(self):
        """
        Auto square-off all open INTRADAY positions at 3:20 PM IST.

        Per spec section 5.6 — ProductType.INTRADAY:
            "Must be closed same day by 3:20 PM IST."

        This cancels any remaining QUEUED intraday orders and
        marks them as EXPIRED. In a full production system, it
        would also place counter-orders to close open positions.
        """
        now_utc = datetime.now(timezone.utc)

        # Only run at 3:20 PM IST = 9:50 AM UTC
        if not (
            now_utc.hour   == INTRADAY_SQUAREOFF_HOUR_UTC and
            now_utc.minute == INTRADAY_SQUAREOFF_MINUTE_UTC
        ):
            return

        logger.info("LimitWatcher: running intraday auto square-off at 3:20 PM IST")

        db = SessionLocal()
        try:
            from app.models.order import OrderEvent

            # Find all open intraday orders
            intraday_open = db.execute(
                select(Order).where(
                    Order.product_type == "intraday",
                    Order.status.in_([OrderStatus.QUEUED, OrderStatus.PENDING_TRIGGER])
                )
            ).scalars().all()

            expired_count = 0
            for order in intraday_open:
                prev_status      = order.status
                order.status     = OrderStatus.EXPIRED
                order.cancelled_at = datetime.now(timezone.utc)
                order.version   += 1

                # Release blocked margin
                from app.services.execution.executor import _release_margin
                _release_margin(db, order, datetime.now(timezone.utc))

                # Write expiry event
                event = OrderEvent(
                    order_id    = order.id,
                    from_status = prev_status.value,
                    to_status   = OrderStatus.EXPIRED.value,
                    reason      = "Intraday auto square-off at 3:20 PM IST",
                    actor       = "limit_watcher",
                )
                db.add(event)
                expired_count += 1

            if expired_count:
                db.commit()
                logger.info(
                    "LimitWatcher: expired %d intraday orders", expired_count
                )

        except Exception as exc:
            logger.exception("LimitWatcher: intraday square-off error: %s", exc)
            db.rollback()
        finally:
            db.close()


# ── Singleton instance used across the application ───────────────────────────
limit_watcher = LimitOrderWatcher()
