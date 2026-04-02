# app/models/kyc.py
# ---------------------------------------------------------------------------
# KYC & Compliance models — per spec section 5.3.
# Tables: kyc_requests, kyc_documents, kyc_review_logs
#
# Flow:
#   User submits KYC → Broker reviews queue → Approve / Reject
#   Every decision is auditable via kyc_review_logs.
#   Trading can be blocked until KYC is approved (policy-driven).
# ---------------------------------------------------------------------------
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime, Enum, ForeignKey, Integer,
    String, Text, Boolean
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.user import User


# ── Enumerations ──────────────────────────────────────────────────────────

class KYCRequestStatus(str, enum.Enum):
    """Per spec section 5.3 — KYC statuses."""
    PENDING               = "pending"
    UNDER_REVIEW          = "under_review"
    APPROVED              = "approved"
    REJECTED              = "rejected"
    RESUBMISSION_REQUIRED = "resubmission_required"


class DocumentType(str, enum.Enum):
    """Supported identity document types for Indian markets."""
    PAN_CARD      = "pan_card"
    AADHAAR       = "aadhaar"
    PASSPORT      = "passport"
    DRIVING_LICENSE = "driving_license"
    VOTER_ID      = "voter_id"
    BANK_STATEMENT= "bank_statement"
    CANCELLED_CHEQUE = "cancelled_cheque"
    PHOTO         = "photo"
    SIGNATURE     = "signature"
    OTHER         = "other"


class ReviewAction(str, enum.Enum):
    """What action the broker/admin took in this review step."""
    SUBMITTED             = "submitted"
    MARKED_UNDER_REVIEW   = "marked_under_review"
    APPROVED              = "approved"
    REJECTED              = "rejected"
    RESUBMISSION_REQUESTED= "resubmission_requested"
    RESUBMITTED           = "resubmitted"


# ── KYCRequest ─────────────────────────────────────────────────────────────

class KYCRequest(Base):
    """
    One KYC application per user.
    A user can resubmit after rejection — a new record is created each time
    to preserve the full audit trail.
    """
    __tablename__ = "kyc_requests"

    id         : Mapped[int]            = mapped_column(Integer, primary_key=True, index=True)
    user_id    : Mapped[int]            = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Which broker is responsible for reviewing this KYC
    broker_id  : Mapped[int | None]     = mapped_column(
        Integer, ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True, index=True
    )

    status     : Mapped[KYCRequestStatus] = mapped_column(
        Enum(KYCRequestStatus), default=KYCRequestStatus.PENDING, nullable=False
    )

    # Reviewer's reason (required on rejection / resubmission request)
    reviewer_notes: Mapped[str | None]  = mapped_column(Text, nullable=True)

    # Who reviewed this request (broker or admin user id)
    reviewed_by: Mapped[int | None]     = mapped_column(Integer, nullable=True)
    reviewed_at: Mapped[datetime | None]= mapped_column(DateTime(timezone=True), nullable=True)

    # Whether the platform policy requires KYC before trading
    trading_blocked: Mapped[bool]       = mapped_column(Boolean, default=True, nullable=False)

    # Expiry support — KYC can be set to expire after a period
    expires_at : Mapped[datetime | None]= mapped_column(DateTime(timezone=True), nullable=True)

    created_at : Mapped[datetime]       = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at : Mapped[datetime]       = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    documents  : Mapped[list[KYCDocument]]   = relationship(
        "KYCDocument", back_populates="kyc_request", cascade="all, delete-orphan"
    )
    review_logs: Mapped[list[KYCReviewLog]]  = relationship(
        "KYCReviewLog", back_populates="kyc_request", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<KYCRequest id={self.id} user_id={self.user_id} status={self.status}>"


# ── KYCDocument ────────────────────────────────────────────────────────────

class KYCDocument(Base):
    """
    Metadata record for a document uploaded as part of a KYC request.
    Per spec: 'PII handling and encryption metadata' — we store
    only references/paths, never raw PII in the DB.
    The actual file bytes live in secure object storage (S3/GCS).
    """
    __tablename__ = "kyc_documents"

    id            : Mapped[int]          = mapped_column(Integer, primary_key=True, index=True)
    kyc_request_id: Mapped[int]          = mapped_column(
        Integer, ForeignKey("kyc_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )

    doc_type      : Mapped[DocumentType] = mapped_column(Enum(DocumentType), nullable=False)

    # File reference — an opaque key/path in secure object storage.
    # Never store a public URL; generate pre-signed URLs on demand.
    storage_key   : Mapped[str]          = mapped_column(String(512), nullable=False)
    original_filename: Mapped[str | None]= mapped_column(String(255), nullable=True)
    mime_type     : Mapped[str | None]   = mapped_column(String(100), nullable=True)
    file_size_bytes: Mapped[int | None]  = mapped_column(Integer, nullable=True)

    # Whether this document has been individually verified
    is_verified   : Mapped[bool]         = mapped_column(Boolean, default=False, nullable=False)

    # Encrypted PII flag — if True, the storage_key points to encrypted data
    is_encrypted  : Mapped[bool]         = mapped_column(Boolean, default=True,  nullable=False)

    uploaded_at   : Mapped[datetime]     = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    kyc_request: Mapped[KYCRequest] = relationship("KYCRequest", back_populates="documents")

    def __repr__(self) -> str:
        return f"<KYCDocument id={self.id} type={self.doc_type} verified={self.is_verified}>"


# ── KYCReviewLog ───────────────────────────────────────────────────────────

class KYCReviewLog(Base):
    """
    Per spec: 'Every KYC decision must be audited.'
    Immutable audit trail — never update, only insert.
    """
    __tablename__ = "kyc_review_logs"

    id            : Mapped[int]        = mapped_column(Integer, primary_key=True, index=True)
    kyc_request_id: Mapped[int]        = mapped_column(
        Integer, ForeignKey("kyc_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )

    action        : Mapped[ReviewAction] = mapped_column(Enum(ReviewAction), nullable=False)
    actor_id      : Mapped[int | None]   = mapped_column(Integer, nullable=True)   # user/broker/admin id
    actor_role    : Mapped[str | None]   = mapped_column(String(50), nullable=True)
    notes         : Mapped[str | None]   = mapped_column(Text, nullable=True)

    # Snapshot of the status before and after this action
    from_status   : Mapped[str | None]   = mapped_column(String(50), nullable=True)
    to_status     : Mapped[str]          = mapped_column(String(50), nullable=False)

    # Per spec audit fields
    ip_address    : Mapped[str | None]   = mapped_column(String(45), nullable=True)
    user_agent    : Mapped[str | None]   = mapped_column(String(512), nullable=True)

    created_at    : Mapped[datetime]     = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    kyc_request: Mapped[KYCRequest] = relationship("KYCRequest", back_populates="review_logs")

    def __repr__(self) -> str:
        return f"<KYCReviewLog id={self.id} action={self.action} to={self.to_status}>"
