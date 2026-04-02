# app/schemas/funding.py
# ---------------------------------------------------------------------------
# Pydantic v2 schemas for Funding & Wallet endpoints.
# Per spec section 5.10 and 7.8.
# ---------------------------------------------------------------------------
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict

from app.models.funding import FundingType, FundingStatus, LedgerEntryType


# ── Funding request schemas ────────────────────────────────────────────────

class FundCreditRequest(BaseModel):
    """Broker credits money into a user's wallet."""
    amount      : float = Field(gt=0, description="Amount to credit — must be positive.")
    currency    : str   = Field(default="INR", max_length=10)
    reason      : str   = Field(min_length=5, max_length=500)
    reference_id: str | None = Field(default=None, max_length=255,
                                      description="Payment gateway transaction ID or internal ref.")


class FundDebitRequest(BaseModel):
    """Broker debits money from a user's wallet (withdrawal etc.)."""
    amount      : float = Field(gt=0)
    currency    : str   = Field(default="INR", max_length=10)
    reason      : str   = Field(min_length=5, max_length=500)
    reference_id: str | None = Field(default=None, max_length=255)


# ── Response schemas ───────────────────────────────────────────────────────

class FundingLogResponse(BaseModel):
    id            : int
    user_id       : int
    broker_id     : int | None
    funding_type  : FundingType
    status        : FundingStatus
    amount        : float
    currency      : str
    reason        : str
    reference_id  : str | None
    balance_after : float | None
    authorized_by : int | None
    created_at    : datetime
    model_config = ConfigDict(from_attributes=True)


class FundingHistoryResponse(BaseModel):
    total   : int
    page    : int
    size    : int
    logs    : list[FundingLogResponse]


class LedgerEntryResponse(BaseModel):
    id             : int
    entry_type     : LedgerEntryType
    amount         : float
    currency       : str
    running_balance: float
    source_type    : str
    source_id      : int | None
    description    : str
    created_at     : datetime
    model_config = ConfigDict(from_attributes=True)


class WalletLedgerResponse(BaseModel):
    total  : int
    page   : int
    size   : int
    entries: list[LedgerEntryResponse]


class WalletBalanceResponse(BaseModel):
    """
    Real-time wallet state for a user.
    Per spec portfolio outputs: cash balance, margin used, free margin, equity.
    """
    user_id        : int
    cash_balance   : float
    margin_used    : float
    free_margin    : float
    unrealized_pnl : float
    equity         : float        # cash + unrealized_pnl
    currency       : str
    as_of          : datetime


class BalanceSnapshotResponse(BaseModel):
    id             : int
    cash_balance   : float
    margin_used    : float
    free_margin    : float
    unrealized_pnl : float
    equity         : float
    currency       : str
    snapshot_reason: str
    is_reconciled  : bool
    snapshot_at    : datetime
    model_config = ConfigDict(from_attributes=True)


class MessageResponse(BaseModel):
    message: str
