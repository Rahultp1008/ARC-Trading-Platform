# app/core/permissions.py
from fastapi import Depends, HTTPException, status
from app.core.dependencies import get_current_user
from app.models.user import User, UserRole, UserStatus

PLATFORM_MAX_LEVERAGE = {
    "equity": 10.0, "fno": 5.0, "crypto": 3.0, "etf": 5.0, "index": 5.0,
}
DEFAULT_LEVERAGE = {
    "equity": 5.0, "fno": 2.0, "crypto": 1.0, "etf": 3.0, "index": 2.0,
}

def require_super_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only Super Admins can perform this action.")
    return current_user

def require_broker_or_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role not in (UserRole.BROKER, UserRole.SUPER_ADMIN):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only Brokers or Super Admins can perform this action.")
    return current_user

def require_active_user(current_user: User = Depends(get_current_user)) -> User:
    if current_user.status not in (UserStatus.ACTIVE, UserStatus.PENDING_KYC):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Your account is suspended or inactive.")
    return current_user

def assert_broker_owns_user(broker: User, target_user: User) -> None:
    if broker.role == UserRole.SUPER_ADMIN:
        return
    if target_user.broker_id != broker.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="You do not have permission to manage this user.")

def validate_leverage_caps(requester, equity, fno, crypto, etf, index) -> None:
    multiplier = 2.0 if requester.role == UserRole.SUPER_ADMIN else 1.0
    checks = [
        ("equity", equity, PLATFORM_MAX_LEVERAGE["equity"] * multiplier),
        ("fno",    fno,    PLATFORM_MAX_LEVERAGE["fno"]    * multiplier),
        ("crypto", crypto, PLATFORM_MAX_LEVERAGE["crypto"] * multiplier),
        ("etf",    etf,    PLATFORM_MAX_LEVERAGE["etf"]    * multiplier),
        ("index",  index,  PLATFORM_MAX_LEVERAGE["index"]  * multiplier),
    ]
    errors = [
        f"{p}: requested {v}x exceeds ceiling of {c}x"
        for p, v, c in checks if v is not None and v > c
    ]
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"leverage_errors": errors})