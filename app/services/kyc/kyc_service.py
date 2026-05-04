# app/services/kyc/kyc_service.py
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

def _role_value(role) -> str:
    """Safely extract string value from a role enum or string."""
    if hasattr(role, "value"):
        return role.value
    return str(role)


def _status_value(s) -> str | None:
    """Safely extract string value from a status enum or string."""
    if s is None:
        return None
    if hasattr(s, "value"):
        return s.value
    return str(s)


def _safe_action(action: ReviewAction) -> str:
    """
    Return a DB-safe action string.
    Maps internal actions that don't exist in the DB enum to valid ones.
    DB enum values: approved, rejected, requested_more_info
    """
    # Map internal actions to valid DB enum values
    action_map = {
        "SUBMITTED"              : "requested_more_info",
        "RESUBMITTED"            : "requested_more_info",
        "MARKED_UNDER_REVIEW"    : "requested_more_info",
        "DOCUMENT_VERIFIED"      : "requested_more_info",
        "RESUBMISSION_REQUESTED" : "requested_more_info",
        "APPROVED"               : "approved",
        "REJECTED"               : "rejected",
        # lowercase versions
        "submitted"              : "requested_more_info",
        "resubmitted"            : "requested_more_info",
        "marked_under_review"    : "requested_more_info",
        "document_verified"      : "requested_more_info",
        "resubmission_requested" : "requested_more_info",
        "approved"               : "approved",
        "rejected"               : "rejected",
        "requested_more_info"    : "requested_more_info",
    }
    raw = action.value if hasattr(action, "value") else str(action)
    return action_map.get(raw, "requested_more_info")


def _write_log(
    db         : Session,
    kyc_request: KYCRequest,
    action     : ReviewAction,
    actor_id   : int | None,
    actor_role,
    notes      : str | None,
    from_status,
    to_status,
    request    : Request | None = None,
) -> KYCReviewLog:
    """Insert one immutable audit log row."""
    log = KYCReviewLog(
        kyc_request_id = kyc_request.id,
        action         = _safe_action(action),        # FIXED: map to valid DB enum
        actor_id       = actor_id,
        actor_role     = _role_value(actor_role),     # FIXED: use .value not str()
        notes          = notes,
        from_status    = _status_value(from_status),
        to_status      = _status_value(to_status),
        ip_address     = request.client.host if request and request.client else None,
        user_agent     = request.headers.get("user-agent") if request else None,
    )
    db.add(log)
    return log


# KYCRequestStatus → User.kyc_status mapping
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

_BROKER_REVIEW_ACTIONS = frozenset(_ACTION_TO_STATUS.keys())


def _get_kyc_with_relations(db: Session, kyc_id: int) -> KYCRequest | None:
    return db.scalar(
        select(KYCRequest)
        .where(KYCRequest.id == kyc_id)
        .options(
            selectinload(KYCRequest.documents),
            selectinload(KYCRequest.review_logs),
        )
    )


def _assert_broker_scope(requester: User, kyc: KYCRequest) -> None:
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
    user = db.get(User, user_id)
    if not user:
        return
    user.kyc_status = _KYC_STATUS_SYNC[new_status]
    if new_status == KYCRequestStatus.APPROVED:
        if user.status == UserStatus.PENDING_KYC:
            user.status = UserStatus.ACTIVE


# ── Pre-signed URL stub ────────────────────────────────────────────────────

def generate_presigned_upload_url(doc_type: DocumentType, user_id: int) -> dict:
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

    kyc_request = KYCRequest(
        user_id   = user.id,
        broker_id = user.broker_id,
        status    = KYCRequestStatus.PENDING,
    )
    db.add(kyc_request)
    db.flush()

    for doc_meta in payload.documents:
        db.add(KYCDocument(
            kyc_request_id   = kyc_request.id,
            doc_type         = doc_meta.doc_type,
            storage_key      = doc_meta.storage_key,
            original_filename= doc_meta.original_filename,
            mime_type        = doc_meta.mime_type,
            file_size_bytes  = doc_meta.file_size_bytes,
            metadata_json    = doc_meta.metadata_json,
        ))

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

    kyc_request = KYCRequest(
        user_id   = user.id,
        broker_id = user.broker_id,
        status    = KYCRequestStatus.PENDING,
    )
    db.add(kyc_request)
    db.flush()

    for doc_meta in payload.documents:
        db.add(KYCDocument(
            kyc_request_id   = kyc_request.id,
            doc_type         = doc_meta.doc_type,
            storage_key      = doc_meta.storage_key,
            original_filename= doc_meta.original_filename,
            mime_type        = doc_meta.mime_type,
            file_size_bytes  = doc_meta.file_size_bytes,
            metadata_json    = doc_meta.metadata_json,
        ))

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

def get_kyc_by_id(db: Session, kyc_id: int, requester: User) -> KYCRequest:
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
    from app.models.user import User as UserModel
    target_user = db.get(UserModel, user_id)
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User id={user_id} not found.",
        )
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
    query = select(KYCRequest)

    if requester.role == UserRole.BROKER:
        query = query.where(KYCRequest.broker_id == requester.id)

    if status_filter:
        query = query.where(KYCRequest.status == status_filter)
    else:
        query = query.where(KYCRequest.status.in_([
            KYCRequestStatus.PENDING, KYCRequestStatus.UNDER_REVIEW
        ]))

    query = query.order_by(KYCRequest.created_at.asc())

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    kyc_requests = db.scalars(
        query.offset((page - 1) * size).limit(size)
    ).all()

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
    kyc = get_kyc_by_id(db, kyc_id, requester)

    if kyc.status not in (KYCRequestStatus.PENDING, KYCRequestStatus.UNDER_REVIEW):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot review a KYC request in '{kyc.status.value}' status. "
                "Only PENDING or UNDER_REVIEW requests can be reviewed."
            ),
        )

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

    kyc.status         = new_status
    kyc.reviewer_notes = payload.notes
    kyc.reviewed_by    = requester.id
    kyc.reviewed_at    = datetime.now(timezone.utc)

    _sync_user_kyc_state(db, kyc.user_id, new_status)

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
    kyc = get_kyc_by_id(db, kyc_id, requester)

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

    if kyc.status not in (KYCRequestStatus.PENDING, KYCRequestStatus.UNDER_REVIEW):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot verify documents on a KYC request in '{kyc.status.value}' status."
            ),
        )

    doc.is_verified = payload.is_verified

    # FIXED: use 'requested_more_info' instead of 'DOCUMENT_VERIFIED'
    # because DB enum only allows: approved, rejected, requested_more_info
    _write_log(
        db, kyc,
        action     = ReviewAction.APPROVED if payload.is_verified else ReviewAction.REJECTED,
        actor_id   = requester.id,
        actor_role = requester.role,
        notes      = (
            f"Document id={doc_id} ({doc.doc_type.value}) marked "
            f"{'verified' if payload.is_verified else 'unverified'}. "
            f"{payload.notes or ''}"
        ).strip(),
        from_status= kyc.status,
        to_status  = kyc.status,
        request    = request,
    )

    db.commit()
    db.refresh(doc)
    return doc
