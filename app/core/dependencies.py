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
    payload = decode_access_token(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token.")
    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active if hasattr(user, "is_active") else False:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found.")
    return user


def require_kyc_approved(current_user: User = Depends(get_current_user)) -> User:
    """
    Trading guard — rejects any request unless the user's KYC is APPROVED.

    Attach this dependency to any trading endpoint (Phase 4+):

        @router.post("/orders")
        def place_order(
            ...,
            current_user: User = Depends(require_kyc_approved),
        ):
            ...

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
