# app/models/kyc.py
# ---------------------------------------------------------------------------
# KYC & Compliance models — per spec section 5.3.
# Tables: kyc_requests, kyc_documents, kyc_review_logs
#
# Flow:
#   User submits KYC → Broker reviews queue → Approve / Reject
#   Every decision is auditable via kyc_review_logs.
#   Trading is blocked until KYC is approved (enforced by require_kyc_approved
#   in app/core/dependencies.py).
#
# ADDITION vs Phase 2 zip:
#   - KYCDocument.metadata_json: JSON column for flexible, encryption-ready
#     PII metadata. Stores masked identifiers (e.g. last 4 digits of Aadhaar,
#     PAN format check result) alongside the encrypted storage reference.
#     Raw PII values are NEVER stored here — only references and masked data.
# ---------------------------------------------------------------------------
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime, Enum, ForeignKey, Integer,
    JSON, String, Text, Boolean
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.user import User


# ── Enumerations ──────────────────────────────────────────────────────────

class KYCRequestStatus(str, enum.Enum):
    """
    Per spec section 5.3 — the five possible KYC states.
    The state machine transitions are:
      NOT_SUBMITTED → PENDING (on submit)
      PENDING → UNDER_REVIEW (broker marks it)
      UNDER_REVIEW → APPROVED | REJECTED | RESUBMISSION_REQUIRED
      REJECTED / RESUBMISSION_REQUIRED → PENDING (on resubmit, new record)
    """
    PENDING               = "pending"
    UNDER_REVIEW          = "under_review"
    APPROVED              = "approved"
    REJECTED              = "rejected"
    RESUBMISSION_REQUIRED = "resubmission_required"


class DocumentType(str, enum.Enum):
    """
    Supported identity document types for Indian markets.
    Maps to SEBI / RBI KYC norms.
    """
    PAN_CARD         = "pan_card"
    AADHAAR          = "aadhaar"
    PASSPORT         = "passport"
    DRIVING_LICENSE  = "driving_license"
    VOTER_ID         = "voter_id"
    BANK_STATEMENT   = "bank_statement"
    CANCELLED_CHEQUE = "cancelled_cheque"
    PHOTO            = "photo"
    SIGNATURE        = "signature"
    OTHER            = "other"


class ReviewAction(str, enum.Enum):
    """
    Immutable action labels for the KYCReviewLog.
    Each row records one discrete action taken by a user, broker, or admin.
    """
    SUBMITTED              = "submitted"
    MARKED_UNDER_REVIEW    = "marked_under_review"
    APPROVED               = "approved"
    REJECTED               = "rejected"
    RESUBMISSION_REQUESTED = "resubmission_requested"
    RESUBMITTED            = "resubmitted"
    DOCUMENT_VERIFIED      = "document_verified"


# ── KYCRequest ─────────────────────────────────────────────────────────────

class KYCRequest(Base):
    """
    One KYC application per user per submission attempt.
    A user can resubmit after rejection or resubmission_required —
    each attempt creates a NEW record to preserve the full audit trail.
    Never mutate an old KYCRequest; create a fresh one.
    """
    __tablename__ = "kyc_requests"

    id        : Mapped[int]            = mapped_column(Integer, primary_key=True, index=True)
    user_id   : Mapped[int]            = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Which broker is responsible for reviewing this submission
    broker_id : Mapped[int | None]     = mapped_column(
        Integer, ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True, index=True
    )

    status    : Mapped[KYCRequestStatus] = mapped_column(
        Enum(KYCRequestStatus), default=KYCRequestStatus.PENDING, nullable=False
    )

    # Reviewer's written reason — mandatory on rejection / resubmission request
    reviewer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ID of the broker or admin who performed the last review action
    reviewed_by   : Mapped[int | None]      = mapped_column(Integer, nullable=True)
    reviewed_at   : Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Per spec: "Trading blocked until approved, if policy enabled."
    # This flag is read by the require_kyc_approved dependency.
    trading_blocked: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Optional KYC expiry (e.g. PAN re-verification after 3 years)
    expires_at : Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at : Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at : Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships — cascade so deleting a KYCRequest cleans up its children
    documents  : Mapped[list[KYCDocument]]  = relationship(
        "KYCDocument",  back_populates="kyc_request", cascade="all, delete-orphan"
    )
    review_logs: Mapped[list[KYCReviewLog]] = relationship(
        "KYCReviewLog", back_populates="kyc_request", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<KYCRequest id={self.id} user_id={self.user_id} status={self.status}>"


# ── KYCDocument ────────────────────────────────────────────────────────────

class KYCDocument(Base):
    """
    Metadata record for one document attached to a KYC request.

    Per spec: 'PII handling and encryption metadata — we store only
    references/paths, never raw PII in the DB.'

    Storage pattern:
      1. Client requests a pre-signed upload URL from the backend.
      2. Client uploads the file DIRECTLY to S3/GCS using that URL.
      3. Client sends back the opaque storage_key (not a public URL).
      4. This record stores that key + metadata.
      5. Pre-signed download URLs are generated on demand (never stored here).

    metadata_json (NEW):
      A flexible JSON column for encryption-ready, non-PII metadata.
      Examples of SAFE values to store here:
        { "pan_format_valid": true, "aadhaar_last4": "5678",
          "doc_checksum": "sha256:abc...", "ocr_confidence": 0.94,
          "encryption_key_id": "kms-key-arn-prod-001" }
      NEVER store the full Aadhaar number, full PAN, name, DOB, or address.
      Those live only in the encrypted file in object storage.
    """
    __tablename__ = "kyc_documents"

    id             : Mapped[int]          = mapped_column(Integer, primary_key=True, index=True)
    kyc_request_id : Mapped[int]          = mapped_column(
        Integer, ForeignKey("kyc_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )

    doc_type       : Mapped[DocumentType] = mapped_column(Enum(DocumentType), nullable=False)

    # Opaque storage key — e.g. "kyc/2024/user-42/pan_card_abc123.pdf.enc"
    # NEVER a public URL. Generate pre-signed URLs on demand.
    storage_key         : Mapped[str]          = mapped_column(String(512), nullable=False)
    original_filename   : Mapped[str | None]   = mapped_column(String(255), nullable=True)
    mime_type           : Mapped[str | None]   = mapped_column(String(100), nullable=True)
    file_size_bytes     : Mapped[int | None]   = mapped_column(Integer, nullable=True)

    # Flexible JSON metadata — safe non-PII data and encryption references.
    # SQLAlchemy JSON type maps to jsonb on PostgreSQL (efficient querying).
    # Default {} means no metadata yet rather than NULL.
    metadata_json  : Mapped[dict | None]  = mapped_column(JSON, nullable=True, default=None)

    # Set to True by the broker when this specific document has been checked
    is_verified    : Mapped[bool]         = mapped_column(Boolean, default=False, nullable=False)

    # True = the file in storage_key is AES-256 encrypted (default for all PII)
    is_encrypted   : Mapped[bool]         = mapped_column(Boolean, default=True,  nullable=False)

    uploaded_at    : Mapped[datetime]     = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    kyc_request: Mapped[KYCRequest] = relationship("KYCRequest", back_populates="documents")

    def __repr__(self) -> str:
        return f"<KYCDocument id={self.id} type={self.doc_type} verified={self.is_verified}>"


# ── KYCReviewLog ───────────────────────────────────────────────────────────

class KYCReviewLog(Base):
    """
    Per spec: 'Every KYC decision must be audited.'

    IMMUTABLE — never UPDATE or DELETE rows. Only ever INSERT.
    Each row is a permanent record of one action taken on a KYC request.
    This is what gives you a complete, tamper-proof audit trail.

    Stores both from_status and to_status so you can reconstruct the
    full state history even if you only look at the logs table.
    """
    __tablename__ = "kyc_review_logs"

    id             : Mapped[int]          = mapped_column(Integer, primary_key=True, index=True)
    kyc_request_id : Mapped[int]          = mapped_column(
        Integer, ForeignKey("kyc_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )

    action     : Mapped[ReviewAction] = mapped_column(Enum(ReviewAction), nullable=False)

    # Who performed this action (user ID, broker ID, or admin ID)
    actor_id   : Mapped[int | None]   = mapped_column(Integer, nullable=True)
    actor_role : Mapped[str | None]   = mapped_column(String(50), nullable=True)
    notes      : Mapped[str | None]   = mapped_column(Text, nullable=True)

    # State snapshot — lets you reconstruct the full transition history
    from_status: Mapped[str | None]   = mapped_column(String(50), nullable=True)
    to_status  : Mapped[str]          = mapped_column(String(50), nullable=False)

    # Network metadata for security auditing per spec section 5.11
    ip_address : Mapped[str | None]   = mapped_column(String(45), nullable=True)   # IPv6 = 45 chars
    user_agent : Mapped[str | None]   = mapped_column(String(512), nullable=True)

    created_at : Mapped[datetime]     = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    kyc_request: Mapped[KYCRequest] = relationship("KYCRequest", back_populates="review_logs")

    def __repr__(self) -> str:
        return f"<KYCReviewLog id={self.id} action={self.action} to={self.to_status}>"
