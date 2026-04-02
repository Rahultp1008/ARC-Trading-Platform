# app/core/dependencies.py
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.user import User, UserStatus, KYCStatus

bearer_scheme = HTTPBearer()

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    payload = decode_access_token(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or expired access token.")
    user = db.get(User, int(payload["sub"]))
    if not user or user.status == UserStatus.SUSPENDED:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="User not found or account is suspended.")
    return user

def require_kyc_approved(current_user: User = Depends(get_current_user)) -> User:
    from app.models.user import UserRole
    if current_user.role in (UserRole.BROKER, UserRole.SUPER_ADMIN):
        return current_user
    if current_user.kyc_status != KYCStatus.APPROVED:
        messages = {
            KYCStatus.NOT_SUBMITTED:          "Please submit your KYC documents before trading.",
            KYCStatus.PENDING:                "Your KYC is under review. Trading enabled once approved.",
            KYCStatus.UNDER_REVIEW:           "Your KYC is being reviewed. Trading enabled once approved.",
            KYCStatus.REJECTED:               "Your KYC was rejected. Please resubmit.",
            KYCStatus.RESUBMISSION_REQUIRED:  "Please resubmit your KYC documents.",
        }
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=messages.get(current_user.kyc_status, "KYC required."))
    return current_user

def require_role(*roles: str):
    def _check(current_user: User = Depends(get_current_user)) -> User:
        if str(current_user.role.value).upper() not in [r.upper() for r in roles]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=f"Access restricted to: {', '.join(roles)}.")
        return current_user
    return _check