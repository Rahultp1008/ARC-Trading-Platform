# =============================================================================
# app/schemas/order.py
# Module 6 — Order Management | ARC Trading Platform
#
# Pydantic v2 schemas for request validation and response serialization.
#
# SCHEMA HIERARCHY:
#   OrderPlaceRequest    → validates the POST /orders body
#   OrderCancelRequest   → validates the POST /orders/{id}/cancel body
#   OrderFilterParams    → validates query params for GET /orders
#   OrderResponse        → full order with events and triggers
#   OrderSummaryResponse → compact order for list view
#   OrderListResponse    → paginated list wrapper
#   MarginPreviewResponse→ GET /orders/margin-preview response
# =============================================================================

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import (
    BaseModel, ConfigDict, Field,
    field_validator, model_validator
)

from app.models.order import (
    OrderSide, OrderStatus, OrderType,
    ProductType, TriggerSide
)


# =============================================================================
# SECTION 1 — REQUEST SCHEMAS
# =============================================================================

class OrderPlaceRequest(BaseModel):
    """
    Body for POST /orders — place a new order.

    The @model_validator runs AFTER individual field validation.
    It enforces business rules that involve multiple fields at once,
    e.g., limit orders must have a price, SL orders must have a trigger.
    """

    # ── What to trade ─────────────────────────────────────────────────────────
    symbol      : str = Field(
        ...,
        description="Canonical symbol e.g. NSE:RELIANCE or CRYPTO:BTCUSDT",
        min_length=3,
        max_length=100
    )
    order_type  : OrderType
    side        : OrderSide
    product_type: ProductType

    # ── Quantity ──────────────────────────────────────────────────────────────
    quantity: Decimal = Field(
        ...,
        gt=0,                                          # Validation check 5: quantity > 0
        description="Number of shares/units — must be > 0 and F&O lot size multiple"
    )

    # ── Price fields — conditionally required based on order_type ─────────────
    price: Optional[Decimal] = Field(
        default=None,
        gt=0,
        description="Limit price — REQUIRED for LIMIT and STOP_LOSS_LIMIT orders"
    )
    trigger_price: Optional[Decimal] = Field(
        default=None,
        gt=0,
        description="SL activation price — REQUIRED for STOP_LOSS_MARKET, STOP_LOSS_LIMIT, TAKE_PROFIT"
    )

    # ── Optional SL/TP to attach to the order ────────────────────────────────
    sl_price: Optional[Decimal] = Field(
        default=None,
        gt=0,
        description="Stop-loss price — creates an OrderTrigger row on acceptance"
    )
    tp_price: Optional[Decimal] = Field(
        default=None,
        gt=0,
        description="Take-profit price — creates an OrderTrigger row on acceptance"
    )

    # ── Client-side idempotency key ───────────────────────────────────────────
    client_order_id: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Your unique key — prevents duplicate orders on network retry"
    )

    # ── Cross-field validations ───────────────────────────────────────────────

    @field_validator("quantity")
    @classmethod
    def quantity_precision(cls, v: Decimal) -> Decimal:
        """Quantity must not have more than 8 decimal places."""
        if v != v.quantize(Decimal("0.00000001")):
            raise ValueError("Quantity must have at most 8 decimal places")
        return v

    @field_validator("price", "trigger_price", "sl_price", "tp_price", mode="before")
    @classmethod
    def price_not_zero(cls, v):
        """Pydantic gt=0 only validates if value is not None — this adds a clear error."""
        if v is not None and Decimal(str(v)) <= 0:
            raise ValueError("Price values must be greater than 0")
        return v

    @model_validator(mode="after")
    def validate_price_requirements(self) -> OrderPlaceRequest:
        """
        CROSS-FIELD RULES:

        LIMIT orders MUST have price.
        STOP_LOSS_LIMIT orders MUST have both price and trigger_price.
        STOP_LOSS_MARKET and TAKE_PROFIT MUST have trigger_price.
        MARKET orders must NOT have a limit price (it would be ignored).

        If sl_price and tp_price are both provided:
            - SL/TP sides must make logical sense (SL < TP for buy orders).
        """
        ot = self.order_type

        # Check 1 — price required for limit-type orders
        if ot == OrderType.LIMIT and self.price is None:
            raise ValueError("price is required for LIMIT orders")

        if ot == OrderType.STOP_LOSS_LIMIT:
            if self.price is None:
                raise ValueError("price is required for STOP_LOSS_LIMIT orders")
            if self.trigger_price is None:
                raise ValueError("trigger_price is required for STOP_LOSS_LIMIT orders")

        # Check 2 — trigger_price required for SL/TP order types
        if ot in (OrderType.STOP_LOSS_MARKET, OrderType.TAKE_PROFIT):
            if self.trigger_price is None:
                raise ValueError(f"trigger_price is required for {ot.value} orders")

        # Check 3 — market orders should not have a price
        if ot == OrderType.MARKET and self.price is not None:
            raise ValueError("MARKET orders must not specify a price — remove the price field")

        # Check 4 — SL must be below TP for buy orders (logical sanity check)
        if self.sl_price and self.tp_price and self.side == OrderSide.BUY:
            if self.sl_price >= self.tp_price:
                raise ValueError(
                    f"For BUY orders, sl_price ({self.sl_price}) must be "
                    f"below tp_price ({self.tp_price})"
                )

        return self

    model_config = ConfigDict(from_attributes=True)


class OrderCancelRequest(BaseModel):
    """Body for POST /orders/{id}/cancel — reason is optional but recommended."""
    reason: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Why the order is being cancelled — stored in order_events"
    )


class OrderFilterParams(BaseModel):
    """
    Query parameters for GET /orders.
    All fields are optional — combine any subset for filtering.
    """
    status      : Optional[OrderStatus]  = Field(default=None, description="Filter by order status")
    symbol      : Optional[str]          = Field(default=None, description="Partial symbol match e.g. RELI")
    side        : Optional[OrderSide]    = Field(default=None)
    order_type  : Optional[OrderType]    = Field(default=None)
    product_type: Optional[ProductType]  = Field(default=None)
    from_date   : Optional[datetime]     = Field(default=None, description="Filter orders placed on/after this date")
    to_date     : Optional[datetime]     = Field(default=None, description="Filter orders placed on/before this date")

    # Pagination
    page: int = Field(default=1, ge=1,   description="Page number starting from 1")
    size: int = Field(default=20, ge=1, le=100, description="Orders per page (max 100)")

    model_config = ConfigDict(from_attributes=True)


# =============================================================================
# SECTION 2 — RESPONSE SCHEMAS
# =============================================================================

class OrderEventResponse(BaseModel):
    """A single state transition record — part of OrderResponse.events."""
    id         : int
    from_status: Optional[str]
    to_status  : str
    reason     : Optional[str]
    actor      : str
    created_at : datetime

    model_config = ConfigDict(from_attributes=True)


class OrderTriggerResponse(BaseModel):
    """A single SL/TP trigger — part of OrderResponse.triggers."""
    id                    : int
    trigger_type          : str
    trigger_side          : TriggerSide
    trigger_price         : Decimal
    order_type_on_trigger : str
    is_triggered          : bool
    triggered_at          : Optional[datetime]
    triggered_at_price    : Optional[Decimal]

    model_config = ConfigDict(from_attributes=True)


class OrderResponse(BaseModel):
    """
    Full order detail — returned by POST /orders and GET /orders/{id}.
    Includes the complete audit trail (events) and trigger instructions.
    """
    id              : int
    user_id         : int
    instrument_id   : str
    symbol          : str
    order_type      : OrderType
    side            : OrderSide
    product_type    : ProductType
    quantity        : Decimal
    filled_qty      : Decimal
    price           : Optional[Decimal]
    trigger_price   : Optional[Decimal]
    average_price   : Optional[Decimal]
    sl_price        : Optional[Decimal]
    tp_price        : Optional[Decimal]
    status          : OrderStatus
    rejection_reason: Optional[str]
    margin_required : Decimal
    margin_blocked  : Decimal
    client_order_id : Optional[str]
    placed_at       : datetime
    accepted_at     : Optional[datetime]
    filled_at       : Optional[datetime]
    cancelled_at    : Optional[datetime]
    updated_at      : datetime

    # Nested relations — show full audit trail and triggers
    events  : list[OrderEventResponse]   = []
    triggers: list[OrderTriggerResponse] = []

    model_config = ConfigDict(from_attributes=True)


class OrderSummaryResponse(BaseModel):
    """
    Compact order info for GET /orders list view.
    Does NOT include events or triggers to keep response small.
    """
    id           : int
    symbol       : str
    order_type   : OrderType
    side         : OrderSide
    product_type : ProductType
    quantity     : Decimal
    filled_qty   : Decimal
    price        : Optional[Decimal]
    average_price: Optional[Decimal]
    status       : OrderStatus
    placed_at    : datetime
    updated_at   : datetime

    model_config = ConfigDict(from_attributes=True)


class OrderListResponse(BaseModel):
    """Paginated list of orders."""
    total : int
    page  : int
    size  : int
    orders: list[OrderSummaryResponse]


class MarginPreviewResponse(BaseModel):
    """
    Response for GET /orders/margin-preview.
    Lets the user see margin required BEFORE they submit an order.
    """
    symbol           : str
    side             : OrderSide
    order_type       : OrderType
    quantity         : Decimal
    estimated_price  : Decimal
    margin_required  : Decimal
    available_balance: Decimal
    sufficient       : bool          # True if user can afford this order
    shortfall        : Optional[Decimal] = None  # How much extra they need
    message          : str
