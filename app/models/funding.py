# app/models/funding.py
# ---------------------------------------------------------------------------
# Funding & Wallet models — per spec section 5.10.
# Tables: funding_logs, wallet_ledgers, balance_snapshots
#
# Core accounting rules from the spec:
#   1. Every balance change MUST produce an immutable ledger entry.
#   2. No direct balance mutation without a log/ledger entry.
#   3. Trade-related blocked margin is separated from free balance.
# ---------------------------------------------------------------------------
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime, Enum, ForeignKey, Integer,
    Numeric, String, Text, Boolean
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.user import User


# ── Enumerations ──────────────────────────────────────────────────────────

class FundingType(str, enum.Enum):
    """
    Per spec section 5.10 — Core Features.
    credit = money added to account
    debit  = money removed from account
    """
    CREDIT      = "credit"
    DEBIT       = "debit"
    MARGIN_BLOCK   = "margin_block"    # locked for an open trade
    MARGIN_RELEASE = "margin_release"  # released after trade closes
    SETTLEMENT     = "settlement"      # end-of-day P&L settlement
    ADJUSTMENT     = "adjustment"      # manual admin correction


class FundingStatus(str, enum.Enum):
    PENDING   = "pending"
    APPROVED  = "approved"
    REJECTED  = "rejected"
    CANCELLED = "cancelled"


class LedgerEntryType(str, enum.Enum):
    """Double-entry style ledger — every action has a debit or credit side."""
    DEBIT  = "debit"   # money leaves the wallet
    CREDIT = "credit"  # money enters the wallet


# ── FundingLog ─────────────────────────────────────────────────────────────

class FundingLog(Base):
    """
    Records every broker-initiated credit or debit for a user.
    Per spec: 'Broker credits/debits user account with reasons.'
    This is the source-of-truth for all non-trade cash movements.
    """
    __tablename__ = "funding_logs"

    id         : Mapped[int]           = mapped_column(Integer, primary_key=True, index=True)
    user_id    : Mapped[int]           = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    broker_id  : Mapped[int | None]    = mapped_column(
        Integer, ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True, index=True
    )

    funding_type: Mapped[FundingType]  = mapped_column(Enum(FundingType), nullable=False)
    status      : Mapped[FundingStatus]= mapped_column(
        Enum(FundingStatus), default=FundingStatus.APPROVED, nullable=False
    )

    # Amount — always positive; direction is determined by funding_type
    amount      : Mapped[float]        = mapped_column(Numeric(18, 2), nullable=False)
    currency    : Mapped[str]          = mapped_column(String(10), default="INR", nullable=False)

    # Human-readable reason (required for audit)
    reason      : Mapped[str]          = mapped_column(String(500), nullable=False)

    # Optional reference (payment gateway txn id, internal transfer id etc.)
    reference_id: Mapped[str | None]   = mapped_column(String(255), nullable=True)

    # Who authorised this funding action
    authorized_by: Mapped[int | None]  = mapped_column(Integer, nullable=True)

    # Balance snapshot after this transaction (denormalised for quick lookup)
    balance_after: Mapped[float | None]= mapped_column(Numeric(18, 2), nullable=True)

    created_at  : Mapped[datetime]     = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    ledger_entries: Mapped[list[WalletLedger]] = relationship(
        "WalletLedger", back_populates="funding_log", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<FundingLog id={self.id} user_id={self.user_id} "
            f"type={self.funding_type} amount={self.amount}>"
        )


# ── WalletLedger ───────────────────────────────────────────────────────────

class WalletLedger(Base):
    """
    Immutable double-entry ledger.
    Per spec: 'Every balance change must produce an immutable ledger entry.'
    Never update or delete rows — only ever INSERT.

    For every funding event, two ledger rows are created:
      - One DEBIT  on the source side
      - One CREDIT on the destination side
    """
    __tablename__ = "wallet_ledgers"

    id             : Mapped[int]             = mapped_column(Integer, primary_key=True, index=True)
    user_id        : Mapped[int]             = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    funding_log_id : Mapped[int | None]      = mapped_column(
        Integer, ForeignKey("funding_logs.id", ondelete="SET NULL"), nullable=True
    )

    entry_type     : Mapped[LedgerEntryType] = mapped_column(Enum(LedgerEntryType), nullable=False)
    amount         : Mapped[float]           = mapped_column(Numeric(18, 2), nullable=False)
    currency       : Mapped[str]             = mapped_column(String(10), default="INR", nullable=False)

    # Running balance AFTER this entry (snapshot for fast reads)
    running_balance: Mapped[float]           = mapped_column(Numeric(18, 2), nullable=False)

    # What triggered this entry — trade, funding, settlement etc.
    source_type    : Mapped[str]             = mapped_column(String(50), nullable=False)
    source_id      : Mapped[int | None]      = mapped_column(Integer, nullable=True)

    description    : Mapped[str]             = mapped_column(String(500), nullable=False)

    created_at     : Mapped[datetime]        = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    funding_log: Mapped[FundingLog | None] = relationship(
        "FundingLog", back_populates="ledger_entries"
    )

    def __repr__(self) -> str:
        return (
            f"<WalletLedger id={self.id} user_id={self.user_id} "
            f"type={self.entry_type} amount={self.amount} balance={self.running_balance}>"
        )


# ── BalanceSnapshot ────────────────────────────────────────────────────────

class BalanceSnapshot(Base):
    """
    Point-in-time snapshot of a user's wallet state.
    Per spec: 'Balance snapshots' for reconciliation support.
    Created daily (EOD) or after significant events.
    """
    __tablename__ = "balance_snapshots"

    id              : Mapped[int]   = mapped_column(Integer, primary_key=True, index=True)
    user_id         : Mapped[int]   = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # The three balance buckets per spec
    cash_balance    : Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    margin_used     : Mapped[float] = mapped_column(Numeric(18, 2), default=0.0, nullable=False)
    free_margin     : Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)

    # Unrealized PnL from open positions at snapshot time
    unrealized_pnl  : Mapped[float] = mapped_column(Numeric(18, 2), default=0.0, nullable=False)

    # Total equity = cash + unrealized PnL
    equity          : Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)

    currency        : Mapped[str]   = mapped_column(String(10), default="INR", nullable=False)
    snapshot_reason : Mapped[str]   = mapped_column(String(100), default="eod", nullable=False)
    is_reconciled   : Mapped[bool]  = mapped_column(Boolean, default=False, nullable=False)

    snapshot_at     : Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return (
            f"<BalanceSnapshot id={self.id} user_id={self.user_id} "
            f"cash={self.cash_balance} equity={self.equity}>"
        )
