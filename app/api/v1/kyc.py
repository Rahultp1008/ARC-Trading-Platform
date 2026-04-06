# app/api/v1/kyc.py
# ---------------------------------------------------------------------------
# KYC & Compliance endpoints.
# Per spec section 7.9:
#   - User:         submit, resubmit, check own status, request upload URL
#   - Broker/Admin: review queue, per-user lookup, detail view,
#                   approve/reject/resubmission, per-document verification
#
# RBAC summary (enforced via dependency injection):
#   get_current_user      → any authenticated user
#   require_broker_or_admin → Broker or Super Admin only
#
# ADDITIONS vs Phase 2 zip:
#   - GET  /kyc/upload-url         → User requests a pre-signed upload URL
#   - POST /kyc/resubmit           → User resubmits after rejection
#   - GET  /kyc/users/{user_id}    → Broker looks up a specific user's KYC
#   - PATCH /kyc/{id}/documents/{doc_id}/verify → Broker verifies one document
# ---------------------------------------------------------------------------

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from app.core.database       import get_db
from app.core.dependencies   import get_current_user
from app.core.permission    import require_broker_or_admin
from app.models.kyc          import DocumentType, KYCRequestStatus
from app.models.user         import User
from app.schemas.kyc import (
    DocumentResponse,
    DocumentVerifyRequest,
    KYCQueueResponse,
    KYCRequestResponse,
    KYCSummaryResponse,
    KYCReviewRequest,
    KYCSubmitRequest,
    MessageResponse,
    PresignedUploadResponse,
    ResubmitKYCRequest,
    UserKYCStatusResponse,
)
from app.services.kyc import kyc_service

router = APIRouter(prefix="/kyc", tags=["KYC & Compliance"])


# ══════════════════════════════════════════════════════════════════════════
# USER ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════

@router.get(
    "/upload-url",
    response_model=PresignedUploadResponse,
    summary="Request a pre-signed document upload URL (User)",
)
def get_upload_url(
    doc_type    : DocumentType,
    current_user: User    = Depends(get_current_user),
):
    """
    NEW — Returns a short-lived pre-signed URL that the client uses to upload
    a document DIRECTLY to object storage (S3/GCS) without routing the file
    bytes through the API server.

    Flow:
      1. Client calls this endpoint to get a pre-signed URL + storage_key.
      2. Client PUTs the file directly to the upload_url (expires in 15 min).
      3. Client includes the storage_key in POST /kyc/submit or /kyc/resubmit.

    Why this pattern?
      The API server never handles raw file bytes, which:
        - Keeps memory usage low under concurrent uploads
        - Ensures PII files are encrypted at rest in object storage
        - Avoids multipart form-data complexity in the API layer
    """
    result = kyc_service.generate_presigned_upload_url(doc_type, current_user.id)
    return PresignedUploadResponse(**result)


@router.post(
    "/submit",
    response_model=KYCRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a KYC application (User — first time only)",
)
def submit_kyc(
    payload     : KYCSubmitRequest,
    request     : Request,
    db          : Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    """
    Authenticated user submits their first KYC application.

    Requires at least one document. Each document must include the storage_key
    returned from GET /kyc/upload-url (the file must be uploaded first).

    Returns the full KYC request with documents and audit log entry.

    If your KYC was rejected, use POST /kyc/resubmit instead.
    """
    kyc = kyc_service.submit_kyc(db, current_user, payload, request)
    return KYCRequestResponse.model_validate(kyc)


@router.post(
    "/resubmit",
    response_model=KYCRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Resubmit KYC after rejection or resubmission request (User)",
)
def resubmit_kyc(
    payload     : ResubmitKYCRequest,
    request     : Request,
    db          : Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    """
    NEW — User resubmits their KYC after it was rejected or flagged for
    resubmission by the broker.

    Difference from POST /submit:
      - Requires a prior rejected/resubmission_required request to exist.
      - Requires resubmission_note explaining what was corrected.
      - Creates a new KYCRequest record (old ones stay for audit trail).

    The broker will see this as a fresh submission in their review queue
    with the resubmission note attached in the audit log.
    """
    kyc = kyc_service.resubmit_kyc(db, current_user, payload, request)
    return KYCRequestResponse.model_validate(kyc)


@router.get(
    "/me",
    response_model=KYCRequestResponse | None,
    summary="Get my current KYC status (User)",
)
def get_my_kyc(
    db          : Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    """
    Returns the most recent KYC request for the logged-in user,
    including all documents and the full review audit trail.
    Returns null if no KYC has been submitted yet.
    """
    kyc = kyc_service.get_my_kyc(db, current_user)
    if not kyc:
        return None
    return KYCRequestResponse.model_validate(kyc)


# ══════════════════════════════════════════════════════════════════════════
# BROKER / ADMIN ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════

@router.get(
    "/queue",
    response_model=KYCQueueResponse,
    summary="KYC review queue — pending applications (Broker/Admin)",
)
def list_kyc_queue(
    page        : int                    = Query(default=1,  ge=1),
    size        : int                    = Query(default=20, ge=1, le=100),
    kyc_status  : KYCRequestStatus | None = Query(default=None, alias="status"),
    db          : Session                = Depends(get_db),
    requester   : User                   = Depends(require_broker_or_admin),
):
    """
    Paginated list of KYC requests for the broker to action.

    Broker scope: only sees KYC requests for their own users.
    Super Admin: sees all platform KYC requests.

    Default (no status filter): shows PENDING + UNDER_REVIEW (actionable items).
    Pass ?status=approved to see approved requests, etc.

    doc_count is now correctly populated (was always 0 in Phase 2).
    """
    total, summaries = kyc_service.list_kyc_queue(
        db, requester, page, size, kyc_status
    )
    return KYCQueueResponse(
        total    = total,
        page     = page,
        size     = size,
        requests = summaries,   # already KYCSummaryResponse objects from service
    )


@router.get(
    "/users/{user_id}",
    response_model=UserKYCStatusResponse | None,
    summary="Get KYC status for a specific user (Broker/Admin)",
)
def get_user_kyc_status(
    user_id  : int,
    db       : Session = Depends(get_db),
    requester: User    = Depends(require_broker_or_admin),
):
    """
    NEW — Broker looks up the current KYC status for a specific user by ID.

    Use this when a user contacts support asking 'why can't I trade?' — the
    broker can quickly pull their KYC status without navigating the queue.

    Broker can only look up users within their scope (403 otherwise).
    Returns null if the user has never submitted KYC.
    """
    kyc = kyc_service.get_user_kyc_for_broker(db, user_id, requester)
    if not kyc:
        return None

    return UserKYCStatusResponse(
        user_id            = user_id,
        kyc_request_id     = kyc.id,
        kyc_request_status = kyc.status,
        doc_count          = len(kyc.documents),
        reviewed_by        = kyc.reviewed_by,
        reviewed_at        = kyc.reviewed_at,
        reviewer_notes     = kyc.reviewer_notes,
        trading_blocked    = kyc.trading_blocked,
        created_at         = kyc.created_at,
        updated_at         = kyc.updated_at,
    )


@router.get(
    "/{kyc_id}",
    response_model=KYCRequestResponse,
    summary="Get full KYC request detail (Broker/Admin)",
)
def get_kyc_detail(
    kyc_id   : int,
    db       : Session = Depends(get_db),
    requester: User    = Depends(require_broker_or_admin),
):
    """
    Full KYC request including all documents and the complete immutable
    review log. Broker can only access KYC for users they own (403 otherwise).
    """
    kyc = kyc_service.get_kyc_by_id(db, kyc_id, requester)
    return KYCRequestResponse.model_validate(kyc)


@router.post(
    "/{kyc_id}/review",
    response_model=KYCRequestResponse,
    summary="Review a KYC application (Broker/Admin)",
)
def review_kyc(
    kyc_id   : int,
    payload  : KYCReviewRequest,
    request  : Request,
    db       : Session = Depends(get_db),
    requester: User    = Depends(require_broker_or_admin),
):
    """
    Broker or Admin takes a review action on a pending KYC request.

    Valid actions:
      marked_under_review     → status becomes UNDER_REVIEW
      approved                → status becomes APPROVED; user may now trade
      rejected                → status becomes REJECTED; user must resubmit
      resubmission_requested  → status becomes RESUBMISSION_REQUIRED

    Every action is written to the immutable KYCReviewLog table.
    When approved, user.status is upgraded from PENDING_KYC to ACTIVE.
    """
    kyc = kyc_service.review_kyc(db, kyc_id, payload, requester, request)
    return KYCRequestResponse.model_validate(kyc)


@router.patch(
    "/{kyc_id}/documents/{doc_id}/verify",
    response_model=DocumentResponse,
    summary="Mark an individual document verified/unverified (Broker/Admin)",
)
def verify_document(
    kyc_id   : int,
    doc_id   : int,
    payload  : DocumentVerifyRequest,
    request  : Request,
    db       : Session = Depends(get_db),
    requester: User    = Depends(require_broker_or_admin),
):
    """
    NEW — Broker reviews and marks individual documents during the KYC review.

    A broker may want to verify a PAN card immediately but wait for the
    Aadhaar to come back from a verification service before approving the
    full request. This endpoint supports that incremental workflow.

    Un-verifying (is_verified=False) requires a notes explanation for audit.
    The KYC request status itself is NOT changed by this endpoint —
    use POST /{kyc_id}/review to make the final approve/reject decision.
    """
    doc = kyc_service.verify_document(db, kyc_id, doc_id, payload, requester, request)
    return DocumentResponse.model_validate(doc)
