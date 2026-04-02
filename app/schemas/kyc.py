# app/schemas/kyc.py
# ---------------------------------------------------------------------------
# Pydantic v2 schemas for KYC & Compliance endpoints.
# Per spec section 5.3 — KYC submission, review workflow, audit.
# ---------------------------------------------------------------------------
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict

from app.models.kyc import DocumentType, KYCRequestStatus, ReviewAction


# ── Document sub-schemas ───────────────────────────────────────────────────

class DocumentUploadRequest(BaseModel):
    """Metadata sent when a user uploads a KYC document."""
    doc_type         : DocumentType
    original_filename: str | None = Field(default=None, max_length=255)
    mime_type        : str | None = Field(default=None, max_length=100)
    file_size_bytes  : int | None = Field(default=None, ge=1)
    # storage_key is set by the backend after the file is saved to object storage
    # The client uploads the file directly to a pre-signed URL and passes back the key


class DocumentResponse(BaseModel):
    id              : int
    doc_type        : DocumentType
    original_filename: str | None
    mime_type       : str | None
    file_size_bytes : int | None
    is_verified     : bool
    is_encrypted    : bool
    uploaded_at     : datetime
    model_config = ConfigDict(from_attributes=True)


# ── KYC Request schemas ────────────────────────────────────────────────────

class KYCSubmitRequest(BaseModel):
    """User submits their KYC application with document metadata."""
    documents: list[DocumentUploadRequest] = Field(
        min_length=1,
        description="At least one document is required to submit KYC."
    )
    # storage_keys parallel to documents — provided by client after upload
    storage_keys: list[str] = Field(
        min_length=1,
        description="Object storage keys for each uploaded document."
    )

    def model_post_init(self, __context) -> None:
        if len(self.documents) != len(self.storage_keys):
            raise ValueError("documents and storage_keys must have the same length.")


class KYCReviewRequest(BaseModel):
    """Broker reviews a pending KYC application."""
    action: ReviewAction = Field(
        description="The review decision: approved, rejected, or resubmission_requested."
    )
    notes : str = Field(
        min_length=5, max_length=1000,
        description="Mandatory notes explaining the decision."
    )


class KYCReviewLogResponse(BaseModel):
    id              : int
    action          : ReviewAction
    actor_id        : int | None
    actor_role      : str | None
    notes           : str | None
    from_status     : str | None
    to_status       : str
    created_at      : datetime
    model_config = ConfigDict(from_attributes=True)


class KYCRequestResponse(BaseModel):
    """Full KYC request detail including documents and review history."""
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
    """Lightweight KYC status card for list views."""
    id        : int
    user_id   : int
    status    : KYCRequestStatus
    created_at: datetime
    updated_at: datetime
    doc_count : int = 0
    model_config = ConfigDict(from_attributes=True)


class KYCQueueResponse(BaseModel):
    """Broker's pending KYC review queue."""
    total   : int
    page    : int
    size    : int
    requests: list[KYCSummaryResponse]


class MessageResponse(BaseModel):
    message: str
