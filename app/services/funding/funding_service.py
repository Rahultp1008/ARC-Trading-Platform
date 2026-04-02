# app/services/funding/funding_service.py
# ---------------------------------------------------------------------------
# Funding & Wallet service layer.
# Per spec section 5.10 accounting rules:
#   1. Every balance change produces an immutable ledger entry.
#   2. No direct balance mutation without a log/ledger.
#   3. Trade-related blocked margin is separated from free balance.
# ---------------------------------------------------------------------------

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models.funding import (
    BalanceSnapshot, FundingLog, FundingStatus,
    FundingType, LedgerEntryType, WalletLedger,
)
from app.models.user import User, UserRole
from app.core.permission import assert_broker_owns_user
from app.schemas.funding import (
    FundCreditRequest, FundDebitRequest, WalletBalanceResponse,
)


# ── Balance helpers ────────────────────────────────────────────────────────

def _get_running_balance(db: Session, user_id: int) -> float:
    """
    Compute current cash balance from the ledger.
    Sums all CREDIT entries and subtracts all DEBIT entries.
    This is the authoritative balance — never stored separately.
    """
    from sqlalchemy import case as sa_case
    result = db.scalar(
        select(
            func.coalesce(
                func.sum(
                    sa_case(
                        (WalletLedger.entry_type == LedgerEntryType.CREDIT,  WalletLedger.amount),
                        (WalletLedger.entry_type == LedgerEntryType.DEBIT,  -WalletLedger.amount),
                        else_=0,
                    )
                ), 0.0
            )
        ).where(WalletLedger.user_id == user_id)
    )
    return float(result or 0.0)


def _get_margin_used(db: Session, user_id: int) -> float:
    """
    Margin currently blocked for open positions.
    Sum of MARGIN_BLOCK minus MARGIN_RELEASE funding entries.
    """
    from sqlalchemy import case as sa_case
    result = db.scalar(
        select(
            func.coalesce(
                func.sum(
                    sa_case(
                        (FundingLog.funding_type == FundingType.MARGIN_BLOCK,   FundingLog.amount),
                        (FundingLog.funding_type == FundingType.MARGIN_RELEASE,-FundingLog.amount),
                        else_=0,
                    )
                ), 0.0
            )
        ).where(
            FundingLog.user_id == user_id,
            FundingLog.status  == FundingStatus.APPROVED,
        )
    )
    return max(0.0, float(result or 0.0))


def _write_ledger(
    db             : Session,
    user_id        : int,
    entry_type     : LedgerEntryType,
    amount         : float,
    running_balance: float,
    source_type    : str,
    source_id      : int | None,
    description    : str,
    funding_log_id : int | None = None,
    currency       : str = "INR",
) -> WalletLedger:
    """
    Core ledger writer — every balance change calls this.
    Per spec: immutable, never updated after creation.
    """
    entry = WalletLedger(
        user_id        = user_id,
        funding_log_id = funding_log_id,
        entry_type     = entry_type,
        amount         = amount,
        currency       = currency,
        running_balance= running_balance,
        source_type    = source_type,
        source_id      = source_id,
        description    = description,
    )
    db.add(entry)
    return entry


# ── Credit ─────────────────────────────────────────────────────────────────

def credit_user(
    db       : Session,
    user_id  : int,
    payload  : FundCreditRequest,
    requester: User,
) -> FundingLog:
    """
    Broker credits money into a user's account.
    Creates a FundingLog and a WalletLedger CREDIT entry atomically.
    """
    target_user = db.get(User, user_id)
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    assert_broker_owns_user(requester, target_user)

    # Compute new balance
    current_balance = _get_running_balance(db, user_id)
    new_balance     = current_balance + payload.amount

    # Create funding log
    log = FundingLog(
        user_id      = user_id,
        broker_id    = requester.id if requester.role == UserRole.BROKER else None,
        funding_type = FundingType.CREDIT,
        status       = FundingStatus.APPROVED,
        amount       = payload.amount,
        currency     = payload.currency,
        reason       = payload.reason,
        reference_id = payload.reference_id,
        authorized_by= requester.id,
        balance_after= new_balance,
    )
    db.add(log)
    db.flush()

    # Write ledger entry
    _write_ledger(
        db,
        user_id        = user_id,
        entry_type     = LedgerEntryType.CREDIT,
        amount         = payload.amount,
        running_balance= new_balance,
        source_type    = "funding",
        source_id      = log.id,
        description    = f"Credit: {payload.reason}",
        funding_log_id = log.id,
        currency       = payload.currency,
    )

    db.commit()
    db.refresh(log)
    return log


# ── Debit ──────────────────────────────────────────────────────────────────

def debit_user(
    db       : Session,
    user_id  : int,
    payload  : FundDebitRequest,
    requester: User,
) -> FundingLog:
    """
    Broker debits money from a user's account (withdrawal etc.).
    Validates that the user has sufficient free balance before deducting.
    """
    target_user = db.get(User, user_id)
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    assert_broker_owns_user(requester, target_user)

    # Free balance check
    current_balance = _get_running_balance(db, user_id)
    margin_used     = _get_margin_used(db, user_id)
    free_balance    = current_balance - margin_used

    if payload.amount > free_balance:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Insufficient free balance. "
                f"Free: {free_balance:.2f}, Requested debit: {payload.amount:.2f}."
            ),
        )

    new_balance = current_balance - payload.amount

    log = FundingLog(
        user_id      = user_id,
        broker_id    = requester.id if requester.role == UserRole.BROKER else None,
        funding_type = FundingType.DEBIT,
        status       = FundingStatus.APPROVED,
        amount       = payload.amount,
        currency     = payload.currency,
        reason       = payload.reason,
        reference_id = payload.reference_id,
        authorized_by= requester.id,
        balance_after= new_balance,
    )
    db.add(log)
    db.flush()

    _write_ledger(
        db,
        user_id        = user_id,
        entry_type     = LedgerEntryType.DEBIT,
        amount         = payload.amount,
        running_balance= new_balance,
        source_type    = "funding",
        source_id      = log.id,
        description    = f"Debit: {payload.reason}",
        funding_log_id = log.id,
        currency       = payload.currency,
    )

    db.commit()
    db.refresh(log)
    return log


# ── Wallet balance ─────────────────────────────────────────────────────────

def get_wallet_balance(
    db       : Session,
    user_id  : int,
    requester: User,
) -> WalletBalanceResponse:
    """
    Real-time wallet state.
    Per spec portfolio outputs: cash balance, margin used, free margin, equity.
    Unrealized PnL is a placeholder — populated by the Portfolio module.
    """
    target_user = db.get(User, user_id)
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    assert_broker_owns_user(requester, target_user)

    cash_balance   = _get_running_balance(db, user_id)
    margin_used    = _get_margin_used(db, user_id)
    free_margin    = max(0.0, cash_balance - margin_used)
    unrealized_pnl = 0.0  # Populated by Portfolio module (Phase 4)
    equity         = cash_balance + unrealized_pnl

    return WalletBalanceResponse(
        user_id       = user_id,
        cash_balance  = round(cash_balance,   2),
        margin_used   = round(margin_used,    2),
        free_margin   = round(free_margin,    2),
        unrealized_pnl= round(unrealized_pnl, 2),
        equity        = round(equity,         2),
        currency      = "INR",
        as_of         = datetime.now(timezone.utc),
    )


# ── Funding history ────────────────────────────────────────────────────────

def list_funding_logs(
    db         : Session,
    user_id    : int,
    requester  : User,
    page       : int = 1,
    size       : int = 20,
    funding_type: FundingType | None = None,
) -> tuple[int, list[FundingLog]]:
    target_user = db.get(User, user_id)
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    assert_broker_owns_user(requester, target_user)

    query = select(FundingLog).where(FundingLog.user_id == user_id)
    if funding_type:
        query = query.where(FundingLog.funding_type == funding_type)
    query = query.order_by(FundingLog.created_at.desc())

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    logs  = db.scalars(query.offset((page - 1) * size).limit(size)).all()
    return total, list(logs)


# ── Wallet ledger ──────────────────────────────────────────────────────────

def get_wallet_ledger(
    db       : Session,
    user_id  : int,
    requester: User,
    page     : int = 1,
    size     : int = 50,
) -> tuple[int, list[WalletLedger]]:
    target_user = db.get(User, user_id)
    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    assert_broker_owns_user(requester, target_user)

    query = (
        select(WalletLedger)
        .where(WalletLedger.user_id == user_id)
        .order_by(WalletLedger.created_at.desc())
    )
    total   = db.scalar(select(func.count()).select_from(query.subquery()))
    entries = db.scalars(query.offset((page - 1) * size).limit(size)).all()
    return total, list(entries)


# ── Balance snapshot ───────────────────────────────────────────────────────

def create_balance_snapshot(
    db     : Session,
    user_id: int,
    reason : str = "manual",
) -> BalanceSnapshot:
    """
    Persist a point-in-time balance snapshot.
    Called by EOD reconciliation worker or on-demand.
    """
    cash_balance   = _get_running_balance(db, user_id)
    margin_used    = _get_margin_used(db, user_id)
    free_margin    = max(0.0, cash_balance - margin_used)
    unrealized_pnl = 0.0   # Portfolio module will update this
    equity         = cash_balance + unrealized_pnl

    snap = BalanceSnapshot(
        user_id        = user_id,
        cash_balance   = cash_balance,
        margin_used    = margin_used,
        free_margin    = free_margin,
        unrealized_pnl = unrealized_pnl,
        equity         = equity,
        snapshot_reason= reason,
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)
    return snap
