# app/schemas/risk.py
from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, ConfigDict

class MarginBreakdown(BaseModel):
    trade_value     : Decimal
    margin_rate     : Decimal
    margin_rate_pct : str
    raw_margin      : Decimal
    minimum_margin  : Decimal
    final_margin    : Decimal
    leverage        : Decimal
    model_config = ConfigDict(from_attributes=True)

class ValidationResult(BaseModel):
    check   : str
    passed  : bool
    message : str
    model_config = ConfigDict(from_attributes=True)

class MarginPreviewResponse(BaseModel):
    symbol          : str
    side            : str
    order_type      : str
    product_type    : str
    quantity        : Decimal
    price           : Optional[Decimal] = None
    ltp             : Optional[Decimal] = None
    effective_price : Decimal
    margin_breakdown: MarginBreakdown
    margin_required : Decimal
    wallet_balance  : Decimal
    margin_used     : Decimal
    available_margin: Decimal
    margin_after    : Decimal
    sufficient      : bool
    can_place       : bool
    validations     : list[ValidationResult]
    rejection_reason: Optional[str] = None
    prices_live     : bool
    computed_at     : datetime
    model_config = ConfigDict(from_attributes=True)

class PositionHealth(BaseModel):
    symbol          : str
    product_type    : str
    net_qty         : Decimal
    avg_cost        : Decimal
    current_ltp     : Optional[Decimal] = None
    market_value    : Optional[Decimal] = None
    margin_required : Decimal
    unrealized_pnl  : Decimal
    health_ratio    : Decimal
    status          : str
    model_config = ConfigDict(from_attributes=True)

class HealthRatioResponse(BaseModel):
    wallet_balance      : Decimal
    total_margin_used   : Decimal
    free_margin         : Decimal
    total_position_value: Decimal
    total_unrealized_pnl: Decimal
    overall_health_ratio: Decimal
    overall_status      : str
    positions           : list[PositionHealth]
    margin_call_warning : bool
    liquidation_risk    : bool
    alerts              : list[str]
    open_positions      : int
    warning_positions   : int
    danger_positions    : int
    computed_at         : datetime
    model_config = ConfigDict(from_attributes=True)

class MarginRequirementResponse(BaseModel):
    symbol            : str
    product_type      : str
    quantity          : Decimal
    effective_price   : Decimal
    trade_value       : Decimal
    margin_rate       : Decimal
    margin_rate_pct   : str
    margin_required   : Decimal
    leverage          : Decimal
    minimum_margin    : Decimal
    tick_size         : Optional[Decimal] = None
    lot_size          : Optional[Decimal] = None
    is_lot_compliant  : bool
    lot_compliance_msg: str
    prices_live       : bool
    computed_at       : datetime
    model_config = ConfigDict(from_attributes=True)
