# app/api/v1/kyc.py
# ---------------------------------------------------------------------------
# KYC & Compliance endpoints.
# Per spec section 7.9:
#   - User: submit KYC, check status
#   - Broker/Admin: review queue, approve/reject
# ---------------------------------------------------------------------------

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.permission import require_broker_or_admin
from app.models.kyc import KYCRequestStatus
from app.models.user import User
from app.schemas.kyc import (
    KYCQueueResponse, KYCRequestResponse,
    KYCSummaryResponse, KYCReviewRequest,
    KYCSubmitRequest, MessageResponse,
)
from app.services.kyc import kyc_service

router = APIRouter(prefix="/kyc", tags=["KYC & Compliance"])


# ══════════════════════════════════════════════════════════════════════════
# USER-FACING ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════

@router.post(
    "/submit",
    response_model=KYCRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a KYC application (User)",
)
def submit_kyc(
    payload     : KYCSubmitRequest,
    request     : Request,
    db          : Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    """
    Authenticated user submits their KYC documents.

    Flow:
      1. Client uploads document files directly to pre-signed object-storage URL.
      2. Client sends back the storage keys alongside document metadata here.
      3. This endpoint creates the KYCRequest and KYCDocument records.
      4. User's kyc_status is set to PENDING.

    Returns the full KYC request with documents and audit log.
    """
    kyc = kyc_service.submit_kyc(db, current_user, payload, request)
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
    summary="KYC review queue (Broker/Admin)",
)
def list_kyc_queue(
    page         : int                    = Query(default=1,  ge=1),
    size         : int                    = Query(default=20, ge=1, le=100),
    kyc_status   : KYCRequestStatus | None = Query(default=None, alias="status"),
    db           : Session                = Depends(get_db),
    requester    : User                   = Depends(require_broker_or_admin),
):
    """
    Broker sees pending KYC requests for their users only.
    Super Admin sees all platform KYC requests.
    Default filter: PENDING + UNDER_REVIEW (actionable items).
    """
    total, items = kyc_service.list_kyc_queue(db, requester, page, size, kyc_status)
    return KYCQueueResponse(
        total   = total,
        page    = page,
        size    = size,
        requests= [KYCSummaryResponse.model_validate(k) for k in items],
    )


@router.get(
    "/{kyc_id}",
    response_model=KYCRequestResponse,
    summary="Get KYC request detail (Broker/Admin)",
)
def get_kyc_detail(
    kyc_id   : int,
    db       : Session = Depends(get_db),
    requester: User    = Depends(require_broker_or_admin),
):
    """
    Full KYC request including all documents and the complete review log.
    Broker can only access KYC for users they own (403 otherwise).
    """
    kyc = kyc_service.get_kyc_by_id(db, kyc_id, requester)
    return KYCRequestResponse.model_validate(kyc)


@router.post(
    "/{kyc_id}/review",
    response_model=KYCRequestResponse,
    summary="Review a KYC application — approve / reject / request resubmission (Broker/Admin)",
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

    Actions:
      - marked_under_review      → moves to UNDER_REVIEW
      - approved                 → moves to APPROVED; user may now trade
      - rejected                 → moves to REJECTED; user must resubmit
      - resubmission_requested   → moves to RESUBMISSION_REQUIRED

    Every action is recorded in the immutable kyc_review_logs table.
    If approved, user.status is set to ACTIVE (if it was PENDING_KYC).
    """
    kyc = kyc_service.review_kyc(db, kyc_id, payload, requester, request)
    return KYCRequestResponse.model_validate(kyc)
