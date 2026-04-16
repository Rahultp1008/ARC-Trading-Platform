# app/core/dependencies.py
# ---------------------------------------------------------------------------
# FIXED: Removed duplicate async get_current_user that was silently
# overwriting the sync version, causing all endpoints to receive a bare
# CurrentUser dataclass instead of the full SQLAlchemy User ORM object.
#
# Root cause: Module 4 additions appended a second async def get_current_user
# at the bottom of this file. In Python the last definition wins, so every
# endpoint was receiving a CurrentUser dataclass (id, role, broker_id only)
# instead of the full User ORM object — breaking auth/me, change-password,
# logout, all of user management, and KYC/compliance.
#
# Fix: One get_current_user (sync, returns full User ORM object).
#      require_role() wraps it cleanly for instrument endpoints.
# ---------------------------------------------------------------------------
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.user import User

bearer_scheme = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    Primary auth dependency — returns the full SQLAlchemy User ORM object.
    Used by auth, user-management, KYC, funding, and all permission helpers.
    """
    payload = decode_access_token(credentials.credentials)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
        )
    user = db.get(User, int(payload["sub"]))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found.",
        )
    return user


def require_kyc_approved(current_user: User = Depends(get_current_user)) -> User:
    """
    Trading guard — rejects unless the user's KYC is APPROVED.
    Brokers and Super Admins bypass this check automatically.
    Per spec section 5.3 — 'Trading blocked until KYC approved.'
    """
    from app.models.user import KYCStatus, UserRole
    if current_user.role in (UserRole.BROKER, UserRole.SUPER_ADMIN):
        return current_user
    if current_user.kyc_status != KYCStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your KYC is under review. Trading will be enabled once approved.",
        )
    return current_user


def require_role(*allowed_roles: str):
    """
    Role-gate factory used by instrument endpoints.

    Usage:
        @router.post("/instruments/sync")
        def trigger_sync(actor: User = Depends(require_role("super_admin"))):
            ...

    Accepts both enum value strings and plain strings, case-insensitive.
    Returns the full User ORM object on success.
    """
    def _check(current_user: User = Depends(get_current_user)) -> User:
        role_str = (
            current_user.role.value
            if hasattr(current_user.role, "value")
            else str(current_user.role)
        ).lower()
        allowed_lower = [r.lower() for r in allowed_roles]
        if role_str not in allowed_lower:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Access denied. "
                    f"Required: {list(allowed_roles)}. "
                    f"Your role: {role_str}"
                ),
            )
        return current_user
    return _check


# Backward-compatible alias — permission.py imports get_current_user from here
# and uses it to build require_super_admin / require_broker_or_admin.
# No changes needed in permission.py.
