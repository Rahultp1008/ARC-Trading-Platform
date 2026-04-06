# app/schemas/kyc.py
# ---------------------------------------------------------------------------
# Pydantic v2 schemas for KYC & Compliance endpoints.
# Per spec section 5.3 — KYC submission, review workflow, audit.
#
# ADDITIONS vs Phase 2 zip:
#   - DocumentResponse now includes metadata_json (mirrors the new model field)
#   - DocumentVerifyRequest: broker marks an individual document as verified
#   - ResubmitKYCRequest: dedicated payload for a user resubmitting after
#     rejection or resubmission_required (same as submit but clearer intent)
#   - UserKYCStatusResponse: lightweight response for broker's user-lookup
#     endpoint (GET /kyc/users/{user_id})
# ---------------------------------------------------------------------------
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, ConfigDict, model_validator

from app.models.kyc import DocumentType, KYCRequestStatus, ReviewAction


# ── Document sub-schemas ───────────────────────────────────────────────────

class DocumentUploadRequest(BaseModel):
    """
    Metadata the client sends when registering an uploaded document.

    Upload flow (pre-signed URL pattern):
      1. Client calls GET /kyc/upload-url?doc_type=pan_card
         → Backend returns a short-lived pre-signed S3 URL + a storage_key
      2. Client PUTs the file bytes directly to S3 using that URL
      3. Client includes the storage_key here so we record the reference
         Without ever handling the raw file bytes ourselves.

    metadata_json: Optional safe, non-PII metadata the client may provide.
    E.g. { "pan_format_valid": true, "doc_checksum": "sha256:abc..." }
    The backend will merge this with server-side metadata before storing.
    NEVER put raw Aadhaar/PAN numbers here.
    """
    doc_type         : DocumentType
    storage_key      : str = Field(
        min_length=5, max_length=512,
        description="Opaque object-storage key returned after pre-signed upload."
    )
    original_filename: str | None = Field(default=None, max_length=255)
    mime_type        : str | None = Field(default=None, max_length=100)
    file_size_bytes  : int | None = Field(default=None, ge=1, le=52_428_800)  # 50MB cap
    metadata_json    : dict[str, Any] | None = Field(
        default=None,
        description="Safe non-PII metadata. Masked IDs, format checks, encryption key refs."
    )


class DocumentResponse(BaseModel):
    """Full document record returned to callers."""
    id               : int
    doc_type         : DocumentType
    original_filename: str | None
    mime_type        : str | None
    file_size_bytes  : int | None
    is_verified      : bool
    is_encrypted     : bool
    metadata_json    : dict[str, Any] | None   # NEW — mirrors KYCDocument.metadata_json
    uploaded_at      : datetime
    # storage_key is intentionally OMITTED from responses —
    # callers must request a fresh pre-signed URL to access the file.
    model_config = ConfigDict(from_attributes=True)


class DocumentVerifyRequest(BaseModel):
    """
    NEW — Broker marks an individual document as verified (or reverses it).
    A broker may review documents one by one before approving the full request.
    notes are required when un-verifying (is_verified=False) for audit trail.
    """
    is_verified: bool
    notes      : str | None = Field(
        default=None, max_length=500,
        description="Required when un-verifying a document. Explain the issue."
    )

    @model_validator(mode="after")
    def notes_required_on_unverify(self) -> "DocumentVerifyRequest":
        if not self.is_verified and not self.notes:
            raise ValueError("notes are required when un-verifying a document.")
        return self


# ── KYC submission schemas ─────────────────────────────────────────────────

class KYCSubmitRequest(BaseModel):
    """
    User submits their first KYC application.
    Each document's storage_key is embedded directly in the DocumentUploadRequest
    (storage_key was moved into DocumentUploadRequest to keep the payload clean
    and avoid the length-mismatch bug in the original two-list design).
    """
    documents: list[DocumentUploadRequest] = Field(
        min_length=1,
        description="At least one document is required to submit KYC."
    )


class ResubmitKYCRequest(BaseModel):
    """
    NEW — Dedicated schema for resubmission after rejection or
    resubmission_required status.

    Functionally the same as KYCSubmitRequest but semantically distinct:
      - KYCSubmitRequest = first-ever submission
      - ResubmitKYCRequest = corrective resubmission with an explanation
    The `resubmission_note` field forces the user to explain what they fixed,
    which is useful for the broker's review log.
    """
    documents        : list[DocumentUploadRequest] = Field(min_length=1)
    resubmission_note: str = Field(
        min_length=10, max_length=500,
        description="Explain what was corrected compared to the previous submission."
    )


# ── Review schemas ─────────────────────────────────────────────────────────

class KYCReviewRequest(BaseModel):
    """
    Broker submits a review decision on a pending KYC request.
    Only the four broker-actionable actions are valid here —
    SUBMITTED and RESUBMITTED are system actions, not broker actions.
    This is validated in the service layer.
    """
    action: ReviewAction = Field(
        description=(
            "marked_under_review | approved | rejected | resubmission_requested"
        )
    )
    notes : str = Field(
        min_length=5, max_length=1000,
        description="Mandatory reviewer notes. Required for all actions."
    )


# ── Response schemas ───────────────────────────────────────────────────────

class KYCReviewLogResponse(BaseModel):
    """One row from the immutable KYCReviewLog table."""
    id         : int
    action     : ReviewAction
    actor_id   : int | None
    actor_role : str | None
    notes      : str | None
    from_status: str | None
    to_status  : str
    created_at : datetime
    model_config = ConfigDict(from_attributes=True)


class KYCRequestResponse(BaseModel):
    """
    Full KYC request detail — includes document list and complete review history.
    Returned after submit, review, and detail lookups.
    """
    id             : int
    user_id        : int
    broker_id      : int | None
    status         : KYCRequestStatus
    reviewer_notes : str | None
    reviewed_by    : int | None
    reviewed_at    : datetime | None
    trading_blocked: bool
    expires_at     : datetime | None
    created_at     : datetime
    updated_at     : datetime
    documents      : list[DocumentResponse]
    review_logs    : list[KYCReviewLogResponse]
    model_config = ConfigDict(from_attributes=True)


class KYCSummaryResponse(BaseModel):
    """
    Lightweight KYC card for list/queue views.
    doc_count is computed in the service layer (not a model field).
    """
    id        : int
    user_id   : int
    status    : KYCRequestStatus
    created_at: datetime
    updated_at: datetime
    doc_count : int = 0   # populated by service layer, defaults to 0
    model_config = ConfigDict(from_attributes=True)


class KYCQueueResponse(BaseModel):
    """Broker's paginated KYC review queue."""
    total   : int
    page    : int
    size    : int
    requests: list[KYCSummaryResponse]


class UserKYCStatusResponse(BaseModel):
    """
    NEW — Compact KYC status view for broker's user-lookup endpoint.
    GET /api/v1/kyc/users/{user_id}
    Shows the user's current KYC status without exposing full document details.
    """
    user_id            : int
    kyc_request_id     : int | None
    kyc_request_status : KYCRequestStatus | None
    doc_count          : int
    reviewed_by        : int | None
    reviewed_at        : datetime | None
    reviewer_notes     : str | None
    trading_blocked    : bool
    created_at         : datetime | None
    updated_at         : datetime | None


class PresignedUploadResponse(BaseModel):
    """
    NEW — Returned when the client requests a pre-signed upload URL.
    The client uploads directly to upload_url, then passes storage_key
    back in the KYCSubmitRequest.
    """
    storage_key : str = Field(description="Opaque key to pass back in the submit payload.")
    upload_url  : str = Field(description="Pre-signed PUT URL. Valid for 15 minutes.")
    expires_in  : int = Field(description="Seconds until the upload URL expires.", default=900)


class MessageResponse(BaseModel):
    message: str
