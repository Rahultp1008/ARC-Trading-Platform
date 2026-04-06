# app/services/kyc/kyc_service.py
# ---------------------------------------------------------------------------
# KYC & Compliance service layer.
# Per spec section 5.3:
#   - User submits KYC → broker reviews queue → approve / reject
#   - Every decision is auditable via kyc_review_logs (insert-only)
#   - Trading blocked until KYC approved (enforced in dependencies.py)
#
# FIXES vs Phase 2 zip:
#   - list_kyc_queue now computes doc_count correctly via a subquery
#     (was always 0 before because it's not a model field)
#   - submit_kyc updated to accept DocumentUploadRequest.storage_key
#     (storage_key was moved into the document object, removing the
#     brittle parallel-list design that could cause index mismatches)
#
# ADDITIONS vs Phase 2 zip:
#   - resubmit_kyc(): dedicated path for post-rejection resubmission
#   - get_user_kyc_for_broker(): broker looks up a specific user's KYC
#   - verify_document(): broker marks an individual document verified/unverified
#   - generate_presigned_upload_url(): stub for pre-signed S3 URL generation
# ---------------------------------------------------------------------------

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, Request, status
from sqlalchemy import select, func
from sqlalchemy.orm import Session, selectinload

from app.models.kyc import (
    DocumentType,
    KYCDocument,
    KYCRequest,
    KYCRequestStatus,
    KYCReviewLog,
    ReviewAction,
)
from app.models.user import KYCStatus, User, UserRole, UserStatus
from app.schemas.kyc import (
    DocumentVerifyRequest,
    KYCReviewRequest,
    KYCSubmitRequest,
    KYCSummaryResponse,
    ResubmitKYCRequest,
)
from app.core.permission import assert_broker_owns_user


# ── Private helpers ────────────────────────────────────────────────────────

def _write_log(
    db         : Session,
    kyc_request: KYCRequest,
    action     : ReviewAction,
    actor_id   : int | None,
    actor_role ,
    notes      : str | None,
    from_status,
    to_status  ,
    request    : Request | None = None,
) -> KYCReviewLog:
    """
    Insert one immutable audit log row.
    Called after EVERY state change — never update existing rows.
    """
    log = KYCReviewLog(
        kyc_request_id = kyc_request.id,
        action         = action,
        actor_id       = actor_id,
        actor_role     = str(actor_role) if actor_role else None,
        notes          = notes,
        from_status    = str(from_status.value if hasattr(from_status, "value") else from_status) if from_status else None,
        to_status      = str(to_status.value if hasattr(to_status, "value") else to_status),
        ip_address     = request.client.host if request and request.client else None,
        user_agent     = request.headers.get("user-agent") if request else None,
    )
    db.add(log)
    return log


# KYCRequestStatus → User.kyc_status mapping (keep in sync with User model)
_KYC_STATUS_SYNC = {
    KYCRequestStatus.PENDING              : KYCStatus.PENDING,
    KYCRequestStatus.UNDER_REVIEW         : KYCStatus.UNDER_REVIEW,
    KYCRequestStatus.APPROVED             : KYCStatus.APPROVED,
    KYCRequestStatus.REJECTED             : KYCStatus.REJECTED,
    KYCRequestStatus.RESUBMISSION_REQUIRED: KYCStatus.RESUBMISSION_REQUIRED,
}

# Which ReviewAction maps to which new KYCRequestStatus
_ACTION_TO_STATUS = {
    ReviewAction.MARKED_UNDER_REVIEW   : KYCRequestStatus.UNDER_REVIEW,
    ReviewAction.APPROVED              : KYCRequestStatus.APPROVED,
    ReviewAction.REJECTED              : KYCRequestStatus.REJECTED,
    ReviewAction.RESUBMISSION_REQUESTED: KYCRequestStatus.RESUBMISSION_REQUIRED,
}

# Actions allowed from the broker review endpoint
_BROKER_REVIEW_ACTIONS = frozenset(_ACTION_TO_STATUS.keys())


def _get_kyc_with_relations(db: Session, kyc_id: int) -> KYCRequest | None:
    """Fetch a KYCRequest with its documents and review_logs eagerly loaded."""
    return db.scalar(
        select(KYCRequest)
        .where(KYCRequest.id == kyc_id)
        .options(
            selectinload(KYCRequest.documents),
            selectinload(KYCRequest.review_logs),
        )
    )


def _assert_broker_scope(requester: User, kyc: KYCRequest) -> None:
    """Raise 403 if a Broker tries to access a KYC not under their scope."""
    if requester.role == UserRole.BROKER and kyc.broker_id != requester.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this KYC request.",
        )


def _sync_user_kyc_state(
    db        : Session,
    user_id   : int,
    new_status: KYCRequestStatus,
) -> None:
    """
    After every KYC state change, update User.kyc_status and User.status
    so the auth/trading layer always sees fresh state.
    """
    user = db.get(User, user_id)
    if not user:
        return
    user.kyc_status = _KYC_STATUS_SYNC[new_status]
    if new_status == KYCRequestStatus.APPROVED:
        # Upgrade from PENDING_KYC → ACTIVE when KYC passes
        if user.status == UserStatus.PENDING_KYC:
            user.status = UserStatus.ACTIVE


# ── Pre-signed URL stub ────────────────────────────────────────────────────

def generate_presigned_upload_url(doc_type: DocumentType, user_id: int) -> dict:
    """
    Generate a pre-signed object-storage upload URL.

    In production this calls boto3 (S3) or google-cloud-storage.
    The stub here returns a fake key so the rest of the flow can be tested
    locally without cloud credentials.

    Real implementation (Phase 3 / hardening):
        import boto3
        s3 = boto3.client("s3")
        key = f"kyc/{user_id}/{doc_type.value}/{uuid.uuid4().hex}.enc"
        url = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": settings.KYC_BUCKET, "Key": key},
            ExpiresIn=900,
        )
        return {"storage_key": key, "upload_url": url, "expires_in": 900}
    """
    storage_key = f"kyc/user-{user_id}/{doc_type.value}/{uuid.uuid4().hex}.enc"
    return {
        "storage_key": storage_key,
        "upload_url" : f"https://storage.example.com/presigned/{storage_key}",
        "expires_in" : 900,
    }


# ── Submit KYC ─────────────────────────────────────────────────────────────

def submit_kyc(
    db     : Session,
    user   : User,
    payload: KYCSubmitRequest,
    request: Request | None = None,
) -> KYCRequest:
    """
    User submits their first KYC application.

    Business rules:
      - Cannot submit if already APPROVED (use a dedicated re-verify flow)
      - Cannot submit if a review is already in-flight (PENDING / UNDER_REVIEW)
      - A new KYCRequest record is created every submission (preserves history)
    """
    # Guard: already approved
    if db.scalar(
        select(KYCRequest)
        .where(KYCRequest.user_id == user.id)
        .where(KYCRequest.status == KYCRequestStatus.APPROVED)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Your KYC is already approved. No resubmission needed.",
        )

    # Guard: already in-flight
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
                f"You already have a KYC request in '{in_flight.status.value}' status. "
                "Wait for the current review to complete, or contact your broker."
            ),
        )

    # Create the KYCRequest
    kyc_request = KYCRequest(
        user_id   = user.id,
        broker_id = user.broker_id,
        status    = KYCRequestStatus.PENDING,
    )
    db.add(kyc_request)
    db.flush()  # populate kyc_request.id before creating children

    # Attach document records
    for doc_meta in payload.documents:
        doc = KYCDocument(
            kyc_request_id   = kyc_request.id,
            doc_type         = doc_meta.doc_type,
            storage_key      = doc_meta.storage_key,
            original_filename= doc_meta.original_filename,
            mime_type        = doc_meta.mime_type,
            file_size_bytes  = doc_meta.file_size_bytes,
            metadata_json    = doc_meta.metadata_json,
        )
        db.add(doc)

    # Immutable audit entry
    _write_log(
        db, kyc_request,
        action     = ReviewAction.SUBMITTED,
        actor_id   = user.id,
        actor_role = user.role,
        notes      = "KYC submitted by user.",
        from_status= None,
        to_status  = KYCRequestStatus.PENDING,
        request    = request,
    )

    # Sync user kyc_status
    _sync_user_kyc_state(db, user.id, KYCRequestStatus.PENDING)

    db.commit()
    db.refresh(kyc_request)
    return kyc_request


# ── Resubmit KYC ──────────────────────────────────────────────────────────

def resubmit_kyc(
    db     : Session,
    user   : User,
    payload: ResubmitKYCRequest,
    request: Request | None = None,
) -> KYCRequest:
    """
    User resubmits after rejection or resubmission_required.

    Why a separate function from submit_kyc?
      - Different guard logic: we REQUIRE an existing rejected/resubmission
        request rather than blocking on one.
      - The resubmission_note is logged so the broker can see what changed.
      - Creates a new KYCRequest record (old one stays for audit trail).
    """
    # Must have a prior rejected / resubmission_required request
    prior = db.scalar(
        select(KYCRequest)
        .where(KYCRequest.user_id == user.id)
        .where(KYCRequest.status.in_([
            KYCRequestStatus.REJECTED, KYCRequestStatus.RESUBMISSION_REQUIRED
        ]))
        .order_by(KYCRequest.created_at.desc())
    )
    if not prior:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "No rejected or resubmission-required KYC found. "
                "Use POST /kyc/submit for your first submission."
            ),
        )

    # Guard: another in-flight request (e.g. concurrent resubmission)
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
            detail="You already have a pending KYC review in progress.",
        )

    # New record — old one preserved for audit trail
    kyc_request = KYCRequest(
        user_id   = user.id,
        broker_id = user.broker_id,
        status    = KYCRequestStatus.PENDING,
    )
    db.add(kyc_request)
    db.flush()

    for doc_meta in payload.documents:
        doc = KYCDocument(
            kyc_request_id   = kyc_request.id,
            doc_type         = doc_meta.doc_type,
            storage_key      = doc_meta.storage_key,
            original_filename= doc_meta.original_filename,
            mime_type        = doc_meta.mime_type,
            file_size_bytes  = doc_meta.file_size_bytes,
            metadata_json    = doc_meta.metadata_json,
        )
        db.add(doc)

    _write_log(
        db, kyc_request,
        action     = ReviewAction.RESUBMITTED,
        actor_id   = user.id,
        actor_role = user.role,
        notes      = f"Resubmission: {payload.resubmission_note}",
        from_status= prior.status,
        to_status  = KYCRequestStatus.PENDING,
        request    = request,
    )

    _sync_user_kyc_state(db, user.id, KYCRequestStatus.PENDING)
    db.commit()
    db.refresh(kyc_request)
    return kyc_request


# ── Get current user's KYC ─────────────────────────────────────────────────

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


# ── Broker: get one KYC by ID ─────────────────────────────────────────────

def get_kyc_by_id(
    db       : Session,
    kyc_id   : int,
    requester: User,
) -> KYCRequest:
    """Fetch a KYC request with scope enforcement."""
    kyc = _get_kyc_with_relations(db, kyc_id)
    if not kyc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"KYC request id={kyc_id} not found.",
        )
    _assert_broker_scope(requester, kyc)
    return kyc


# ── Broker: get KYC for a specific user ───────────────────────────────────

def get_user_kyc_for_broker(
    db       : Session,
    user_id  : int,
    requester: User,
) -> KYCRequest | None:
    """
    NEW — Broker looks up the current KYC status for a specific user.

    Used by GET /kyc/users/{user_id}.
    Brokers can only look up users they own — scope enforced via User model.
    Returns the most recent KYC request or None if none exists.
    """
    # Verify the user exists and is in the broker's scope
    from app.models.user import User as UserModel
    target_user = db.get(UserModel, user_id)
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User id={user_id} not found.",
        )
    # Reuse the broker-scope check from permissions module
    assert_broker_owns_user(requester, target_user)

    return db.scalar(
        select(KYCRequest)
        .where(KYCRequest.user_id == user_id)
        .options(
            selectinload(KYCRequest.documents),
            selectinload(KYCRequest.review_logs),
        )
        .order_by(KYCRequest.created_at.desc())
    )


# ── Broker: list review queue ──────────────────────────────────────────────

def list_kyc_queue(
    db           : Session,
    requester    : User,
    page         : int = 1,
    size         : int = 20,
    status_filter: KYCRequestStatus | None = None,
) -> tuple[int, list[KYCSummaryResponse]]:
    """
    Broker sees pending KYC for their users only.
    Super Admin sees all platform KYC.

    FIX: doc_count is now correctly populated via a subquery count rather than
    defaulting to 0. The previous version returned KYCRequest ORM objects and
    relied on Pydantic's `doc_count: int = 0` default — it was always 0.
    """
    query = select(KYCRequest)

    if requester.role == UserRole.BROKER:
        query = query.where(KYCRequest.broker_id == requester.id)

    if status_filter:
        query = query.where(KYCRequest.status == status_filter)
    else:
        # Default: show only actionable items (not approved/rejected)
        query = query.where(KYCRequest.status.in_([
            KYCRequestStatus.PENDING, KYCRequestStatus.UNDER_REVIEW
        ]))

    query = query.order_by(KYCRequest.created_at.asc())

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    kyc_requests = db.scalars(
        query.offset((page - 1) * size).limit(size)
    ).all()

    # Compute doc_count per request using a single batch query
    # (avoids N+1 queries — one query for all counts at once)
    if kyc_requests:
        request_ids = [k.id for k in kyc_requests]
        count_rows = db.execute(
            select(KYCDocument.kyc_request_id, func.count(KYCDocument.id).label("cnt"))
            .where(KYCDocument.kyc_request_id.in_(request_ids))
            .group_by(KYCDocument.kyc_request_id)
        ).all()
        doc_counts = {row.kyc_request_id: row.cnt for row in count_rows}
    else:
        doc_counts = {}

    summaries = [
        KYCSummaryResponse(
            id        = k.id,
            user_id   = k.user_id,
            status    = k.status,
            created_at= k.created_at,
            updated_at= k.updated_at,
            doc_count = doc_counts.get(k.id, 0),
        )
        for k in kyc_requests
    ]
    return total, summaries


# ── Broker: review a KYC request ──────────────────────────────────────────

def review_kyc(
    db       : Session,
    kyc_id   : int,
    payload  : KYCReviewRequest,
    requester: User,
    request  : Request | None = None,
) -> KYCRequest:
    """
    Broker or Admin takes a review action on a KYC request.
    Per spec: 'Every KYC decision must be audited.'

    Allowed transitions:
      PENDING / UNDER_REVIEW → UNDER_REVIEW       (marked_under_review)
      PENDING / UNDER_REVIEW → APPROVED            (approved)
      PENDING / UNDER_REVIEW → REJECTED            (rejected)
      PENDING / UNDER_REVIEW → RESUBMISSION_REQ.  (resubmission_requested)
    """
    kyc = get_kyc_by_id(db, kyc_id, requester)

    # Only actionable from PENDING or UNDER_REVIEW
    if kyc.status not in (KYCRequestStatus.PENDING, KYCRequestStatus.UNDER_REVIEW):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot review a KYC request in '{kyc.status.value}' status. "
                "Only PENDING or UNDER_REVIEW requests can be reviewed."
            ),
        )

    # Validate this is a broker-facing action (not SUBMITTED/RESUBMITTED)
    if payload.action not in _BROKER_REVIEW_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"'{payload.action.value}' is not a valid review action. "
                f"Use one of: {', '.join(a.value for a in _BROKER_REVIEW_ACTIONS)}."
            ),
        )

    from_status = kyc.status
    new_status  = _ACTION_TO_STATUS[payload.action]

    # Update KYC request
    kyc.status         = new_status
    kyc.reviewer_notes = payload.notes
    kyc.reviewed_by    = requester.id
    kyc.reviewed_at    = datetime.now(timezone.utc)

    # Sync user.kyc_status and user.status
    _sync_user_kyc_state(db, kyc.user_id, new_status)

    # Immutable audit entry
    _write_log(
        db, kyc,
        action     = payload.action,
        actor_id   = requester.id,
        actor_role = requester.role,
        notes      = payload.notes,
        from_status= from_status,
        to_status  = new_status,
        request    = request,
    )

    db.commit()
    db.refresh(kyc)
    return kyc


# ── Broker: verify individual document ────────────────────────────────────

def verify_document(
    db        : Session,
    kyc_id    : int,
    doc_id    : int,
    payload   : DocumentVerifyRequest,
    requester : User,
    request   : Request | None = None,
) -> KYCDocument:
    """
    NEW — Broker marks a single document as verified (or reverses it).

    A broker may want to verify documents one-by-one during their review
    before making a final approve/reject decision on the whole request.
    The verification state change is logged in KYCReviewLog for auditability.
    """
    kyc = get_kyc_by_id(db, kyc_id, requester)

    # Find the document
    doc = db.scalar(
        select(KYCDocument)
        .where(KYCDocument.id == doc_id)
        .where(KYCDocument.kyc_request_id == kyc_id)
    )
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document id={doc_id} not found in KYC request id={kyc_id}.",
        )

    # Only allow verification on in-flight requests
    if kyc.status not in (KYCRequestStatus.PENDING, KYCRequestStatus.UNDER_REVIEW):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot verify documents on a KYC request in '{kyc.status.value}' status."
            ),
        )

    doc.is_verified = payload.is_verified

    _write_log(
        db, kyc,
        action     = ReviewAction.DOCUMENT_VERIFIED,
        actor_id   = requester.id,
        actor_role = requester.role,
        notes      = (
            f"Document id={doc_id} ({doc.doc_type.value}) marked "
            f"{'verified' if payload.is_verified else 'unverified'}. "
            f"{payload.notes or ''}"
        ).strip(),
        from_status= kyc.status,
        to_status  = kyc.status,   # KYC status itself doesn't change
        request    = request,
    )

    db.commit()
    db.refresh(doc)
    return doc
