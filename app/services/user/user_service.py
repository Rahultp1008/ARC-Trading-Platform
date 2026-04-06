# app/services/user/user_service.py
# ---------------------------------------------------------------------------
# Service layer for User management.
# Per spec section 5.2:
#   - Brokers can only CRUD users with their broker_id.
#   - Super Admin can CRUD any user.
#   - Leverage caps are validated against platform ceilings.
# ---------------------------------------------------------------------------

from sqlalchemy import select, func
from sqlalchemy.orm import Session
from fastapi import HTTPException, status

from app.core.security import hash_password, verify_password
from app.core.permission import (
    DEFAULT_LEVERAGE, PLATFORM_MAX_LEVERAGE,
    assert_broker_owns_user, validate_leverage_caps,
)
from app.models.broker import Broker
from app.models.user import User, UserRole, UserStatus, KYCStatus
from app.models.user_settings import UserSettings
from app.schemas.user import (
    UserCreateRequest, UserLeverageUpdateRequest,
    UserStatusUpdateRequest, UserUpdateRequest,
)


# ── Helpers ────────────────────────────────────────────────────────────────

def _check_max_users(db: Session, broker: Broker) -> None:
    """Enforce broker-level user cap (0 = unlimited)."""
    if broker.max_users == 0:
        return
    current_count = db.scalar(
        select(func.count(User.id)).where(User.broker_id == broker.id)
    ) or 0
    if current_count >= broker.max_users:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Broker has reached their maximum user limit of {broker.max_users}. "
                "Contact a Super Admin to increase the limit."
            ),
        )


def _apply_leverage(user: User, lp, requester: User) -> None:
    """Apply leverage profile fields to a user model instance."""
    if lp is None:
        return
    validate_leverage_caps(
        requester,
        lp.max_leverage_equity,
        lp.max_leverage_fno,
        lp.max_leverage_crypto,
        lp.max_leverage_etf,
        lp.max_leverage_index,
    )
    if lp.max_leverage_equity is not None:
        user.max_leverage_equity = lp.max_leverage_equity
    if lp.max_leverage_fno    is not None:
        user.max_leverage_fno    = lp.max_leverage_fno
    if lp.max_leverage_crypto  is not None:
        user.max_leverage_crypto  = lp.max_leverage_crypto
    if lp.max_leverage_etf     is not None:
        user.max_leverage_etf     = lp.max_leverage_etf
    if lp.max_leverage_index   is not None:
        user.max_leverage_index   = lp.max_leverage_index


def _apply_product_access(user: User, pa) -> None:
    if pa is None:
        return
    if pa.can_trade_equity is not None: user.can_trade_equity = pa.can_trade_equity
    if pa.can_trade_fno    is not None: user.can_trade_fno    = pa.can_trade_fno
    if pa.can_trade_crypto is not None: user.can_trade_crypto = pa.can_trade_crypto
    if pa.can_trade_etf    is not None: user.can_trade_etf    = pa.can_trade_etf
    if pa.can_trade_index  is not None: user.can_trade_index  = pa.can_trade_index


# ── Create ─────────────────────────────────────────────────────────────────

def create_user(
    db       : Session,
    payload  : UserCreateRequest,
    requester: User,              # the authenticated Broker or Super Admin
) -> User:
    """
    Create a new User scoped to the requesting broker.
    Super Admin must supply an explicit broker_id via a separate
    admin endpoint (not this service directly).
    """
    # ── Determine which broker owns this user ──────────────────────────
    if requester.role == UserRole.BROKER:
        broker_id = requester.id
        broker    = db.get(Broker, broker_id)
        if not broker or not broker.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your broker account is inactive.",
            )
        _check_max_users(db, broker)
    else:
        # Super Admin flow — broker_id is not set here; use admin endpoint
        broker_id = None

    # ── Uniqueness checks ──────────────────────────────────────────────
    if db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A user with email '{payload.email}' already exists.",
        )
    if payload.phone:
        if db.scalar(select(User).where(User.phone == payload.phone)):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A user with phone '{payload.phone}' already exists.",
            )

    # ── Build user ─────────────────────────────────────────────────────
    user = User(
        email           = payload.email,
        full_name       = payload.full_name,
        hashed_password = hash_password(payload.password),
        phone           = payload.phone,
        broker_id       = broker_id,
        notes           = payload.notes,
        role            = UserRole.USER,
        status          = UserStatus.PENDING_ACTIVATION,
        kyc_status      = KYCStatus.NOT_SUBMITTED,
        # Set defaults from platform config
        max_leverage_equity  = DEFAULT_LEVERAGE["equity"],
        max_leverage_fno     = DEFAULT_LEVERAGE["fno"],
        max_leverage_crypto  = DEFAULT_LEVERAGE["crypto"],
        max_leverage_etf     = DEFAULT_LEVERAGE["etf"],
        max_leverage_index   = DEFAULT_LEVERAGE["index"],
    )

    # Apply any leverage or product overrides from payload
    _apply_leverage(user, payload.leverage_profile, requester)
    _apply_product_access(user, payload.product_access)

    db.add(user)
    db.flush()   # get user.id before creating settings

    # Create default UserSettings
    db.add(UserSettings(user_id=user.id))

    db.commit()
    db.refresh(user)
    return user


# ── Read ───────────────────────────────────────────────────────────────────

def get_user_by_id(db: Session, user_id: int, requester: User) -> User:
    """
    Fetch one user. Broker can only fetch their own users.
    Super Admin can fetch any user.
    """
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with id={user_id} not found.",
        )
    assert_broker_owns_user(requester, user)
    return user


def list_users(
    db        : Session,
    requester : User,
    page      : int = 1,
    size      : int = 20,
    status_filter: UserStatus | None = None,
    kyc_filter   : KYCStatus  | None = None,
    search       : str | None = None,
) -> tuple[int, list[User]]:
    """
    Paginated user list.
    Broker sees ONLY their users. Super Admin sees ALL users.
    """
    query = select(User)

    # Scope by broker
    if requester.role == UserRole.BROKER:
        query = query.where(User.broker_id == requester.id)

    # Optional filters
    if status_filter:
        query = query.where(User.status == status_filter)
    if kyc_filter:
        query = query.where(User.kyc_status == kyc_filter)
    if search:
        like = f"%{search}%"
        query = query.where(
            User.email.ilike(like) | User.full_name.ilike(like)
        )

    total   = db.scalar(select(func.count()).select_from(query.subquery()))
    users   = db.scalars(query.offset((page - 1) * size).limit(size)).all()
    return total, list(users)


# ── Update ─────────────────────────────────────────────────────────────────

def update_user(
    db       : Session,
    user_id  : int,
    payload  : UserUpdateRequest,
    requester: User,
) -> User:
    user = get_user_by_id(db, user_id, requester)

    # Phone uniqueness
    if payload.phone and payload.phone != user.phone:
        if db.scalar(select(User).where(User.phone == payload.phone)):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Phone '{payload.phone}' is already in use.",
            )

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(user, field, value)

    db.commit()
    db.refresh(user)
    return user


def update_user_status(
    db       : Session,
    user_id  : int,
    payload  : UserStatusUpdateRequest,
    requester: User,
) -> User:
    """Suspend or re-activate a user. Appends reason to notes."""
    user = get_user_by_id(db, user_id, requester)
    user.status = payload.status
    user.notes  = f"Status changed to {payload.status}: {payload.reason}"
    db.commit()
    db.refresh(user)
    return user


def update_user_leverage(
    db       : Session,
    user_id  : int,
    payload  : UserLeverageUpdateRequest,
    requester: User,
) -> User:
    """
    Assign leverage caps to a user.
    Per spec 5.2 — Risk Profile Assignment.
    Broker cannot exceed platform ceilings; Super Admin gets 2x headroom.
    """
    user = get_user_by_id(db, user_id, requester)
    _apply_leverage(user, payload.leverage_profile, requester)
    _apply_product_access(user, payload.product_access)
    db.commit()
    db.refresh(user)
    return user


# ── Delete ─────────────────────────────────────────────────────────────────

def delete_user(db: Session, user_id: int, requester: User) -> None:
    """
    Soft-delete via status=suspended is preferred.
    Hard-delete is available but only if the user has no trade history.
    (Trade history check is a placeholder — extend when Orders module exists.)
    """
    user = get_user_by_id(db, user_id, requester)
    # Placeholder: in production check for orders/positions before deleting
    db.delete(user)
    db.commit()


# ── Password change (admin-initiated) ─────────────────────────────────────

def admin_reset_password(
    db          : Session,
    user_id     : int,
    new_password: str,
    requester   : User,
) -> User:
    """
    Broker or Admin resets a user's password.
    The user is forced to log in again (all refresh tokens revoked separately).
    """
    user = get_user_by_id(db, user_id, requester)
    user.hashed_password = hash_password(new_password)
    db.commit()
    db.refresh(user)
    return user
