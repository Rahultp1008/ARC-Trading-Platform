# app/services/kyc/kyc_service.py
# ---------------------------------------------------------------------------
# KYC & Compliance service layer.
# Per spec section 5.3:
#   - User submits KYC → broker reviews queue → approve / reject
#   - Every decision is auditable via kyc_review_logs
#   - Trading blocked until KYC approved (if policy enabled)
# ---------------------------------------------------------------------------

from datetime import datetime, timezone

from fastapi import HTTPException, status, Request
from sqlalchemy import select, func
from sqlalchemy.orm import Session, selectinload

from app.models.kyc import (
    KYCDocument, KYCRequest, KYCRequestStatus,
    KYCReviewLog, ReviewAction,
)
from app.models.user import User, KYCStatus, UserRole, UserStatus
from app.schemas.kyc import KYCSubmitRequest, KYCReviewRequest
from app.core.permission import assert_broker_owns_user


# ── Submit KYC ─────────────────────────────────────────────────────────────

def submit_kyc(
    db     : Session,
    user   : User,
    payload: KYCSubmitRequest,
    request: Request | None = None,
) -> KYCRequest:
    """
    User submits their KYC application.
    A user may only have one active (non-rejected) KYC request at a time.
    After rejection, a fresh submission creates a new KYCRequest record.
    """
    # Block re-submission if already approved
    existing = db.scalar(
        select(KYCRequest)
        .where(KYCRequest.user_id == user.id)
        .where(KYCRequest.status == KYCRequestStatus.APPROVED)
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Your KYC is already approved.",
        )

    # Block if there is a pending / under-review request
    in_flight = db.scalar(
        select(KYCRequest)
        .where(KYCRequest.user_id == user.id)
        .where(KYCRequest.status.in_([
            KYCRequestStatus.PENDING, KYCRequestStatus.UNDER_REVIEW
        ]))
    )
    if in_flight:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"You already have a KYC request in status '{in_flight.status}'. "
                "Wait for the current review to complete before resubmitting."
            ),
        )

    # Create the KYC request
    kyc_request = KYCRequest(
        user_id    = user.id,
        broker_id  = user.broker_id,
        status     = KYCRequestStatus.PENDING,
    )
    db.add(kyc_request)
    db.flush()   # get id before adding documents

    # Attach document records
    for doc_meta, storage_key in zip(payload.documents, payload.storage_keys):
        doc = KYCDocument(
            kyc_request_id   = kyc_request.id,
            doc_type         = doc_meta.doc_type,
            storage_key      = storage_key,
            original_filename= doc_meta.original_filename,
            mime_type        = doc_meta.mime_type,
            file_size_bytes  = doc_meta.file_size_bytes,
        )
        db.add(doc)

    # Write audit log entry
    _write_log(
        db, kyc_request,
        action      = ReviewAction.SUBMITTED,
        actor_id    = user.id,
        actor_role  = user.role,
        notes       = "KYC submitted by user.",
        from_status = None,
        to_status   = KYCRequestStatus.PENDING,
        request     = request,
    )

    # Update user's KYC status
    user.kyc_status = KYCStatus.PENDING
    db.commit()
    db.refresh(kyc_request)
    return kyc_request


# ── Get KYC for current user ───────────────────────────────────────────────

def get_my_kyc(db: Session, user: User) -> KYCRequest | None:
    """Return the most recent KYC request for the authenticated user."""
    return db.scalar(
        select(KYCRequest)
        .where(KYCRequest.user_id == user.id)
        .options(
            selectinload(KYCRequest.documents),
            selectinload(KYCRequest.review_logs),
        )
        .order_by(KYCRequest.created_at.desc())
    )


# ── Broker: list review queue ──────────────────────────────────────────────

def list_kyc_queue(
    db       : Session,
    requester: User,
    page     : int = 1,
    size     : int = 20,
    status_filter: KYCRequestStatus | None = None,
) -> tuple[int, list[KYCRequest]]:
    """
    Broker sees pending KYC requests for their users only.
    Super Admin sees all platform KYC requests.
    """
    query = select(KYCRequest)

    if requester.role == UserRole.BROKER:
        query = query.where(KYCRequest.broker_id == requester.id)

    if status_filter:
        query = query.where(KYCRequest.status == status_filter)
    else:
        # Default: show actionable items
        query = query.where(KYCRequest.status.in_([
            KYCRequestStatus.PENDING, KYCRequestStatus.UNDER_REVIEW
        ]))

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(
        query.order_by(KYCRequest.created_at.asc())
             .offset((page - 1) * size)
             .limit(size)
    ).all()
    return total, list(items)


# ── Broker: get one KYC request ────────────────────────────────────────────

def get_kyc_by_id(
    db        : Session,
    kyc_id    : int,
    requester : User,
) -> KYCRequest:
    kyc = db.scalar(
        select(KYCRequest)
        .where(KYCRequest.id == kyc_id)
        .options(
            selectinload(KYCRequest.documents),
            selectinload(KYCRequest.review_logs),
        )
    )
    if not kyc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"KYC request {kyc_id} not found.",
        )

    # Scope check — broker can only see their users' KYC
    if requester.role == UserRole.BROKER and kyc.broker_id != requester.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this KYC request.",
        )
    return kyc


# ── Broker: review a KYC request ──────────────────────────────────────────

def review_kyc(
    db       : Session,
    kyc_id   : int,
    payload  : KYCReviewRequest,
    requester: User,
    request  : Request | None = None,
) -> KYCRequest:
    """
    Broker or Admin approves / rejects / requests resubmission.
    Per spec: 'Every KYC decision must be audited.'
    """
    kyc = get_kyc_by_id(db, kyc_id, requester)

    # Only actionable on pending / under_review states
    if kyc.status not in (KYCRequestStatus.PENDING, KYCRequestStatus.UNDER_REVIEW):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot review a KYC request in status '{kyc.status}'.",
        )

    # Validate action transitions
    valid_actions = {
        ReviewAction.MARKED_UNDER_REVIEW,
        ReviewAction.APPROVED,
        ReviewAction.REJECTED,
        ReviewAction.RESUBMISSION_REQUESTED,
    }
    if payload.action not in valid_actions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid review action '{payload.action}' for this endpoint.",
        )

    from_status = kyc.status

    # Map action → new status
    action_to_status = {
        ReviewAction.MARKED_UNDER_REVIEW   : KYCRequestStatus.UNDER_REVIEW,
        ReviewAction.APPROVED              : KYCRequestStatus.APPROVED,
        ReviewAction.REJECTED              : KYCRequestStatus.REJECTED,
        ReviewAction.RESUBMISSION_REQUESTED: KYCRequestStatus.RESUBMISSION_REQUIRED,
    }
    new_status = action_to_status[payload.action]

    kyc.status         = new_status
    kyc.reviewer_notes = payload.notes
    kyc.reviewed_by    = requester.id
    kyc.reviewed_at    = datetime.now(timezone.utc)

    # Sync user.kyc_status with KYC model KYCStatus
    user = db.get(User, kyc.user_id)
    if user:
        kyc_status_map = {
            KYCRequestStatus.PENDING              : KYCStatus.PENDING,
            KYCRequestStatus.UNDER_REVIEW         : KYCStatus.UNDER_REVIEW,
            KYCRequestStatus.APPROVED             : KYCStatus.APPROVED,
            KYCRequestStatus.REJECTED             : KYCStatus.REJECTED,
            KYCRequestStatus.RESUBMISSION_REQUIRED: KYCStatus.RESUBMISSION_REQUIRED,
        }
        user.kyc_status = kyc_status_map[new_status]

        # Per spec: trading blocked until KYC approved
        if new_status == KYCRequestStatus.APPROVED:
            if user.status == UserStatus.PENDING_KYC:
                user.status = UserStatus.ACTIVE

    # Write immutable audit log
    _write_log(
        db, kyc,
        action      = payload.action,
        actor_id    = requester.id,
        actor_role  = requester.role,
        notes       = payload.notes,
        from_status = from_status,
        to_status   = new_status,
        request     = request,
    )

    db.commit()
    db.refresh(kyc)
    return kyc


# ── Internal: write audit log ─────────────────────────────────────────────

def _write_log(
    db         : Session,
    kyc_request: KYCRequest,
    action     : ReviewAction,
    actor_id   : int | None,
    actor_role : str | None,
    notes      : str | None,
    from_status,
    to_status,
    request    : Request | None,
) -> KYCReviewLog:
    log = KYCReviewLog(
        kyc_request_id = kyc_request.id,
        action         = action,
        actor_id       = actor_id,
        actor_role     = str(actor_role) if actor_role else None,
        notes          = notes,
        from_status    = str(from_status) if from_status else None,
        to_status      = str(to_status),
        ip_address     = request.client.host if request and request.client else None,
        user_agent     = request.headers.get("user-agent") if request else None,
    )
    db.add(log)
    return log
