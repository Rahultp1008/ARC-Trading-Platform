# app/api/v1/funding.py
# ---------------------------------------------------------------------------
# Funding & Wallet endpoints.
# Per spec section 7.8:
#   - Broker credits/debits user accounts
#   - Users and brokers can view wallet balance and ledger history
# ---------------------------------------------------------------------------

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.permission import require_broker_or_admin
from app.models.funding import FundingType
from app.models.user import User
from app.schemas.funding import (
    BalanceSnapshotResponse,
    FundCreditRequest, FundDebitRequest,
    FundingHistoryResponse, FundingLogResponse,
    LedgerEntryResponse, MessageResponse,
    WalletBalanceResponse, WalletLedgerResponse,
)
from app.services.funding import funding_service

router = APIRouter(prefix="/funding", tags=["Funding & Wallet"])


# ══════════════════════════════════════════════════════════════════════════
# BROKER / ADMIN FUNDING ACTIONS
# ══════════════════════════════════════════════════════════════════════════

@router.post(
    "/users/{user_id}/credit",
    response_model=FundingLogResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Credit funds into a user's wallet (Broker/Admin)",
)
def credit_user(
    user_id  : int,
    payload  : FundCreditRequest,
    db       : Session = Depends(get_db),
    requester: User    = Depends(require_broker_or_admin),
):
    """
    Broker adds funds to a user's wallet.
    Creates:
      - A FundingLog record (source-of-truth for the transaction)
      - A WalletLedger CREDIT entry (immutable balance record)
    Returns the funding log with the new balance.
    """
    log = funding_service.credit_user(db, user_id, payload, requester)
    return FundingLogResponse.model_validate(log)


@router.post(
    "/users/{user_id}/debit",
    response_model=FundingLogResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Debit funds from a user's wallet (Broker/Admin)",
)
def debit_user(
    user_id  : int,
    payload  : FundDebitRequest,
    db       : Session = Depends(get_db),
    requester: User    = Depends(require_broker_or_admin),
):
    """
    Broker removes funds from a user's wallet.
    Validates sufficient free balance (cash minus margin_used) before deducting.
    Creates a WalletLedger DEBIT entry.
    """
    log = funding_service.debit_user(db, user_id, payload, requester)
    return FundingLogResponse.model_validate(log)


# ══════════════════════════════════════════════════════════════════════════
# WALLET BALANCE
# ══════════════════════════════════════════════════════════════════════════

@router.get(
    "/users/{user_id}/balance",
    response_model=WalletBalanceResponse,
    summary="Get real-time wallet balance (Broker/Admin/User)",
)
def get_wallet_balance(
    user_id  : int,
    db       : Session = Depends(get_db),
    requester: User    = Depends(require_broker_or_admin),
):
    """
    Returns the current wallet state:
      - cash_balance   : total cash from ledger
      - margin_used    : locked for open trades
      - free_margin    : cash available to trade with
      - unrealized_pnl : from open positions (populated by Portfolio module)
      - equity         : cash + unrealized_pnl
    """
    return funding_service.get_wallet_balance(db, user_id, requester)


@router.get(
    "/me/balance",
    response_model=WalletBalanceResponse,
    summary="Get my wallet balance (User)",
)
def get_my_balance(
    db          : Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    """User views their own wallet balance."""
    return funding_service.get_wallet_balance(db, current_user.id, current_user)


# ══════════════════════════════════════════════════════════════════════════
# FUNDING HISTORY
# ══════════════════════════════════════════════════════════════════════════

@router.get(
    "/users/{user_id}/logs",
    response_model=FundingHistoryResponse,
    summary="Funding history for a user (Broker/Admin)",
)
def get_funding_logs(
    user_id      : int,
    page         : int             = Query(default=1,  ge=1),
    size         : int             = Query(default=20, ge=1, le=100),
    funding_type : FundingType | None = Query(default=None),
    db           : Session         = Depends(get_db),
    requester    : User            = Depends(require_broker_or_admin),
):
    """
    Paginated list of all funding transactions for a user.
    Filterable by funding_type (credit, debit, margin_block etc.).
    """
    total, logs = funding_service.list_funding_logs(
        db, user_id, requester, page, size, funding_type
    )
    return FundingHistoryResponse(
        total=total, page=page, size=size,
        logs=[FundingLogResponse.model_validate(l) for l in logs],
    )


@router.get(
    "/me/logs",
    response_model=FundingHistoryResponse,
    summary="My funding history (User)",
)
def get_my_funding_logs(
    page        : int  = Query(default=1,  ge=1),
    size        : int  = Query(default=20, ge=1, le=100),
    db          : Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    total, logs = funding_service.list_funding_logs(
        db, current_user.id, current_user, page, size
    )
    return FundingHistoryResponse(
        total=total, page=page, size=size,
        logs=[FundingLogResponse.model_validate(l) for l in logs],
    )


# ══════════════════════════════════════════════════════════════════════════
# WALLET LEDGER
# ══════════════════════════════════════════════════════════════════════════

@router.get(
    "/users/{user_id}/ledger",
    response_model=WalletLedgerResponse,
    summary="Immutable wallet ledger for a user (Broker/Admin)",
)
def get_wallet_ledger(
    user_id  : int,
    page     : int     = Query(default=1,  ge=1),
    size     : int     = Query(default=50, ge=1, le=200),
    db       : Session = Depends(get_db),
    requester: User    = Depends(require_broker_or_admin),
):
    """
    Every credit and debit entry in immutable order.
    Each entry shows the running balance after the transaction.
    This is the authoritative audit trail for all balance changes.
    """
    total, entries = funding_service.get_wallet_ledger(db, user_id, requester, page, size)
    return WalletLedgerResponse(
        total=total, page=page, size=size,
        entries=[LedgerEntryResponse.model_validate(e) for e in entries],
    )


@router.get(
    "/me/ledger",
    response_model=WalletLedgerResponse,
    summary="My wallet ledger (User)",
)
def get_my_ledger(
    page        : int  = Query(default=1,  ge=1),
    size        : int  = Query(default=50, ge=1, le=200),
    db          : Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    total, entries = funding_service.get_wallet_ledger(
        db, current_user.id, current_user, page, size
    )
    return WalletLedgerResponse(
        total=total, page=page, size=size,
        entries=[LedgerEntryResponse.model_validate(e) for e in entries],
    )


# ══════════════════════════════════════════════════════════════════════════
# BALANCE SNAPSHOT (Admin / Reconciliation)
# ══════════════════════════════════════════════════════════════════════════

@router.post(
    "/users/{user_id}/snapshot",
    response_model=BalanceSnapshotResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a balance snapshot for a user (Admin)",
)
def create_snapshot(
    user_id  : int,
    reason   : str     = Query(default="manual", max_length=100),
    db       : Session = Depends(get_db),
    requester: User    = Depends(require_broker_or_admin),
):
    """
    Manually trigger a balance snapshot for reconciliation.
    Normally called by the EOD reconciliation background worker.
    """
    snap = funding_service.create_balance_snapshot(db, user_id, reason)
    return BalanceSnapshotResponse.model_validate(snap)

