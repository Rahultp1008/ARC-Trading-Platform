# app/api/v1/admin.py
# ---------------------------------------------------------------------------
# Super Admin API — Broker Management.
# Per spec section 7.2: Admin APIs — broker CRUD, system settings, audits.
# ALL endpoints here require the caller to be a Super Admin.
# ---------------------------------------------------------------------------

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permission import require_super_admin
from app.models.user import User
from app.schemas.broker import (
    BrokerCreateRequest, BrokerListResponse,
    BrokerResponse, BrokerSuspendRequest, BrokerUpdateRequest,
)
from app.services.user import broker_service

router = APIRouter(prefix="/admin", tags=["Super Admin — Broker Management"])


# ── POST /admin/brokers — Create broker ────────────────────────────────────
@router.post(
    "/brokers",
    response_model=BrokerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new Broker (Super Admin only)",
)
def create_broker(
    payload   : BrokerCreateRequest,
    db        : Session = Depends(get_db),
    _admin    : User    = Depends(require_super_admin),   # enforces role
):
    """
    Super Admin creates a new broker account.
    Per spec: 'Only super admin can create brokers.'
    Validates email/phone uniqueness automatically.
    """
    broker = broker_service.create_broker(db, payload)
    count  = broker_service.get_broker_user_count(db, broker.id)
    data   = BrokerResponse.model_validate(broker)
    data.user_count = count
    return data


# ── GET /admin/brokers — List all brokers ──────────────────────────────────
@router.get(
    "/brokers",
    response_model=BrokerListResponse,
    summary="List all Brokers (Super Admin only)",
)
def list_brokers(
    page      : int        = Query(default=1,  ge=1),
    size      : int        = Query(default=20, ge=1, le=100),
    is_active : bool | None = Query(default=None),
    db        : Session    = Depends(get_db),
    _admin    : User       = Depends(require_super_admin),
):
    total, brokers = broker_service.list_brokers(db, page, size, is_active)
    broker_responses = []
    for b in brokers:
        r = BrokerResponse.model_validate(b)
        r.user_count = broker_service.get_broker_user_count(db, b.id)
        broker_responses.append(r)
    return BrokerListResponse(total=total, page=page, size=size, brokers=broker_responses)


# ── GET /admin/brokers/{id} — Get one broker ───────────────────────────────
@router.get(
    "/brokers/{broker_id}",
    response_model=BrokerResponse,
    summary="Get Broker details (Super Admin only)",
)
def get_broker(
    broker_id: int,
    db       : Session = Depends(get_db),
    _admin   : User    = Depends(require_super_admin),
):
    broker = broker_service.get_broker_by_id(db, broker_id)
    data   = BrokerResponse.model_validate(broker)
    data.user_count = broker_service.get_broker_user_count(db, broker_id)
    return data


# ── PATCH /admin/brokers/{id} — Update broker ──────────────────────────────
@router.patch(
    "/brokers/{broker_id}",
    response_model=BrokerResponse,
    summary="Update Broker details (Super Admin only)",
)
def update_broker(
    broker_id: int,
    payload  : BrokerUpdateRequest,
    db       : Session = Depends(get_db),
    _admin   : User    = Depends(require_super_admin),
):
    broker = broker_service.update_broker(db, broker_id, payload)
    data   = BrokerResponse.model_validate(broker)
    data.user_count = broker_service.get_broker_user_count(db, broker_id)
    return data


# ── POST /admin/brokers/{id}/suspend ──────────────────────────────────────
@router.post(
    "/brokers/{broker_id}/suspend",
    response_model=BrokerResponse,
    summary="Suspend a Broker (Super Admin only)",
)
def suspend_broker(
    broker_id: int,
    payload  : BrokerSuspendRequest,
    db       : Session = Depends(get_db),
    _admin   : User    = Depends(require_super_admin),
):
    """
    Suspending a broker prevents them from logging in.
    Their existing users remain untouched — a separate admin decision.
    """
    broker = broker_service.suspend_broker(db, broker_id, payload.reason)
    data   = BrokerResponse.model_validate(broker)
    data.user_count = broker_service.get_broker_user_count(db, broker_id)
    return data


# ── POST /admin/brokers/{id}/activate ─────────────────────────────────────
@router.post(
    "/brokers/{broker_id}/activate",
    response_model=BrokerResponse,
    summary="Reactivate a suspended Broker (Super Admin only)",
)
def activate_broker(
    broker_id: int,
    db       : Session = Depends(get_db),
    _admin   : User    = Depends(require_super_admin),
):
    broker = broker_service.activate_broker(db, broker_id)
    data   = BrokerResponse.model_validate(broker)
    data.user_count = broker_service.get_broker_user_count(db, broker_id)
    return data


# ── DELETE /admin/brokers/{id} — Delete broker ────────────────────────────
@router.delete(
    "/brokers/{broker_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a Broker (Super Admin only — only if no users)",
)
def delete_broker(
    broker_id: int,
    db       : Session = Depends(get_db),
    _admin   : User    = Depends(require_super_admin),
):
    """
    Hard delete. Only succeeds if the broker has zero users.
    Prefer suspension over deletion for brokers who have had users.
    """
    broker_service.delete_broker(db, broker_id)
