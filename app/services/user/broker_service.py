# app/services/user/broker_service.py
# ---------------------------------------------------------------------------
# Service layer for Broker management.
# Only Super Admin can call these functions (enforced at API layer).
# Per spec section 5.2 — "Only super admin can create brokers."
# ---------------------------------------------------------------------------

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fastapi import HTTPException, status

from app.core.security import hash_password
from app.models.broker import Broker
from app.models.user import User
from app.schemas.broker import BrokerCreateRequest, BrokerUpdateRequest


# ── Create ─────────────────────────────────────────────────────────────────

def create_broker(db: Session, payload: BrokerCreateRequest) -> Broker:
    """
    Create a new Broker account.
    Called exclusively by Super Admin.
    Validates email and phone uniqueness before inserting.
    """
    # Email uniqueness check
    if db.scalar(select(Broker).where(Broker.email == payload.email)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A broker with email '{payload.email}' already exists.",
        )

    # Phone uniqueness check (if provided)
    if payload.phone:
        if db.scalar(select(Broker).where(Broker.phone == payload.phone)):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A broker with phone '{payload.phone}' already exists.",
            )

    broker = Broker(
        email           = payload.email,
        full_name       = payload.full_name,
        hashed_password = hash_password(payload.password),
        phone           = payload.phone,
        company_name    = payload.company_name,
        max_users       = payload.max_users,
        admin_notes     = payload.admin_notes,
    )
    db.add(broker)
    db.commit()
    db.refresh(broker)
    return broker


# ── Read ───────────────────────────────────────────────────────────────────

def get_broker_by_id(db: Session, broker_id: int) -> Broker:
    broker = db.get(Broker, broker_id)
    if not broker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Broker with id={broker_id} not found.",
        )
    return broker


def list_brokers(
    db      : Session,
    page    : int = 1,
    size    : int = 20,
    is_active: bool | None = None,
) -> tuple[int, list[Broker]]:
    """Return paginated broker list with optional active filter."""
    query = select(Broker)
    if is_active is not None:
        query = query.where(Broker.is_active == is_active)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    brokers = db.scalars(
        query.offset((page - 1) * size).limit(size)
    ).all()
    return total, list(brokers)


def get_broker_user_count(db: Session, broker_id: int) -> int:
    """How many users does this broker currently own?"""
    return db.scalar(
        select(func.count(User.id)).where(User.broker_id == broker_id)
    ) or 0


# ── Update ─────────────────────────────────────────────────────────────────

def update_broker(db: Session, broker_id: int, payload: BrokerUpdateRequest) -> Broker:
    broker = get_broker_by_id(db, broker_id)

    # Phone uniqueness if changing
    if payload.phone and payload.phone != broker.phone:
        if db.scalar(select(Broker).where(Broker.phone == payload.phone)):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Phone '{payload.phone}' is already in use.",
            )

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(broker, field, value)

    db.commit()
    db.refresh(broker)
    return broker


# ── Suspend / Activate ─────────────────────────────────────────────────────

def suspend_broker(db: Session, broker_id: int, reason: str) -> Broker:
    """
    Suspend a broker. All their users are NOT automatically suspended —
    that is a separate admin decision.
    """
    broker = get_broker_by_id(db, broker_id)
    if not broker.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Broker is already suspended.",
        )
    broker.is_active   = False
    broker.admin_notes = f"Suspended: {reason}"
    db.commit()
    db.refresh(broker)
    return broker


def activate_broker(db: Session, broker_id: int) -> Broker:
    broker = get_broker_by_id(db, broker_id)
    if broker.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Broker is already active.",
        )
    broker.is_active = True
    db.commit()
    db.refresh(broker)
    return broker


# ── Delete ─────────────────────────────────────────────────────────────────

def delete_broker(db: Session, broker_id: int) -> None:
    """
    Hard-delete a broker only if they have zero users.
    If they have users, require suspension instead.
    """
    broker = get_broker_by_id(db, broker_id)
    user_count = get_broker_user_count(db, broker_id)
    if user_count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot delete broker — they still own {user_count} user(s). "
                "Suspend the broker instead, or reassign their users first."
            ),
        )
    db.delete(broker)
    db.commit()
