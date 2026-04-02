# app/api/v1/users.py
# ---------------------------------------------------------------------------
# User Management API — Broker and Super Admin endpoints.
# Per spec section 7.3: Broker APIs — user creation, update, listing, etc.
#
# Key RBAC rules enforced here:
#   - Broker can only CRUD users where user.broker_id == broker.id
#   - Super Admin can CRUD any user
#   - Regular User cannot call any endpoint in this router
# ---------------------------------------------------------------------------

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permission import require_broker_or_admin
from app.models.user import User, UserStatus, KYCStatus
from app.schemas.user import (
    MessageResponse,
    UserCreateRequest,
    UserLeverageUpdateRequest,
    UserListResponse,
    UserResponse,
    UserStatusUpdateRequest,
    UserSummaryResponse,
    UserUpdateRequest,
)
from app.services.user import user_service

router = APIRouter(prefix="/users", tags=["User Management (Broker & Admin)"])


# ── POST /users — Create user ──────────────────────────────────────────────
@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new User (Broker creates under their scope)",
)
def create_user(
    payload  : UserCreateRequest,
    db       : Session = Depends(get_db),
    requester: User    = Depends(require_broker_or_admin),
):
    """
    Broker creates a user automatically scoped to themselves.
    Super Admin uses POST /admin/users/{broker_id}/users to create under a
    specific broker (handled by the admin router).

    Validates:
      - Email uniqueness
      - Phone uniqueness (if provided)
      - Broker max_users cap
      - Leverage values within platform ceilings
    """
    user = user_service.create_user(db, payload, requester)
    return UserResponse.model_validate(user)


# ── GET /users — List users ────────────────────────────────────────────────
@router.get(
    "",
    response_model=UserListResponse,
    summary="List Users (Broker sees their own; Admin sees all)",
)
def list_users(
    page        : int              = Query(default=1,  ge=1),
    size        : int              = Query(default=20, ge=1, le=100),
    status_filter: UserStatus | None = Query(default=None, alias="status"),
    kyc_filter   : KYCStatus  | None = Query(default=None, alias="kyc_status"),
    search       : str | None       = Query(default=None, description="Search by email or name"),
    db           : Session          = Depends(get_db),
    requester    : User             = Depends(require_broker_or_admin),
):
    """
    Broker sees ONLY users where user.broker_id == broker.id.
    Super Admin sees ALL users platform-wide.
    Supports filtering by status, KYC state, and free-text search.
    """
    total, users = user_service.list_users(
        db, requester, page, size, status_filter, kyc_filter, search
    )
    return UserListResponse(
        total=total, page=page, size=size,
        users=[UserSummaryResponse.model_validate(u) for u in users],
    )


# ── GET /users/{id} — Get one user ────────────────────────────────────────
@router.get(
    "/{user_id}",
    response_model=UserResponse,
    summary="Get User details",
)
def get_user(
    user_id  : int,
    db       : Session = Depends(get_db),
    requester: User    = Depends(require_broker_or_admin),
):
    """
    Returns full user profile.
    Broker can only fetch users they own (403 otherwise).
    """
    user = user_service.get_user_by_id(db, user_id, requester)
    return UserResponse.model_validate(user)


# ── PATCH /users/{id} — Update user ───────────────────────────────────────
@router.patch(
    "/{user_id}",
    response_model=UserResponse,
    summary="Update User profile",
)
def update_user(
    user_id  : int,
    payload  : UserUpdateRequest,
    db       : Session = Depends(get_db),
    requester: User    = Depends(require_broker_or_admin),
):
    user = user_service.update_user(db, user_id, payload, requester)
    return UserResponse.model_validate(user)


# ── PATCH /users/{id}/status — Change user status ─────────────────────────
@router.patch(
    "/{user_id}/status",
    response_model=UserResponse,
    summary="Suspend or activate a User",
)
def update_user_status(
    user_id  : int,
    payload  : UserStatusUpdateRequest,
    db       : Session = Depends(get_db),
    requester: User    = Depends(require_broker_or_admin),
):
    """
    Broker can suspend/activate their own users.
    Super Admin can suspend any user on the platform.
    A reason is mandatory and is stored on the user record.
    """
    user = user_service.update_user_status(db, user_id, payload, requester)
    return UserResponse.model_validate(user)


# ── PATCH /users/{id}/leverage — Assign risk/leverage profile ─────────────
@router.patch(
    "/{user_id}/leverage",
    response_model=UserResponse,
    summary="Set leverage caps and product access for a User",
)
def update_user_leverage(
    user_id  : int,
    payload  : UserLeverageUpdateRequest,
    db       : Session = Depends(get_db),
    requester: User    = Depends(require_broker_or_admin),
):
    """
    Per spec section 5.2 — Risk Profile Assignment:
    Broker configures leverage/margin preferences within allowed bounds.
    Super Admin can set higher leverage (up to 2x platform ceiling).

    Also controls which product types (equity, F&O, crypto etc.) the user
    is allowed to trade.
    """
    user = user_service.update_user_leverage(db, user_id, payload, requester)
    return UserResponse.model_validate(user)


# ── POST /users/{id}/reset-password — Admin password reset ────────────────
@router.post(
    "/{user_id}/reset-password",
    response_model=MessageResponse,
    summary="Admin-initiated password reset for a User",
)
def admin_reset_password(
    user_id     : int,
    new_password: str,
    db          : Session = Depends(get_db),
    requester   : User    = Depends(require_broker_or_admin),
):
    """
    Broker or Admin resets a user's password.
    The user must log in again — their existing sessions should be revoked
    (handled by the auth module's token revocation logic).
    """
    user_service.admin_reset_password(db, user_id, new_password, requester)
    return MessageResponse(message="Password reset successfully. User must log in again.")


# ── DELETE /users/{id} — Delete user ──────────────────────────────────────
@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a User (prefer suspension over deletion)",
)
def delete_user(
    user_id  : int,
    db       : Session = Depends(get_db),
    requester: User    = Depends(require_broker_or_admin),
):
    """
    Hard-delete a user. Prefer status=suspended for auditable deactivation.
    Will fail if the user has existing trade history (placeholder guard).
    """
    user_service.delete_user(db, user_id, requester)
