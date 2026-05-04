# app/core/permissions.py
# ---------------------------------------------------------------------------
# Platform-wide permission rules and leverage cap constants.
# Per spec section 5.2 — leverage cap by product type.
# Brokers may assign leverage UP TO these platform ceilings.
# Super Admin can override with higher values.
# ---------------------------------------------------------------------------

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User, UserRole


# ── Platform-wide leverage ceilings ───────────────────────────────────────
# Per spec: Broker configures leverage within allowed bounds.
# These are the hard maximums enforced by the platform.

PLATFORM_MAX_LEVERAGE = {
    "equity" : 10.0,   # NSE equities intraday
    "fno"    :  5.0,   # Futures & Options
    "crypto" :  3.0,   # Crypto paper trading
    "etf"    :  5.0,   # Exchange Traded Funds
    "index"  :  5.0,   # Indices
}

# Default leverage assigned when a broker creates a new user
DEFAULT_LEVERAGE = {
    "equity" : 5.0,
    "fno"    : 2.0,
    "crypto" : 1.0,
    "etf"    : 3.0,
    "index"  : 2.0,
}


# ── Role-check dependencies ────────────────────────────────────────────────

def require_super_admin(current_user: User = Depends(get_current_user)) -> User:
    """
    Dependency — only Super Admin may proceed.
    Used on broker-management endpoints.
    """
    if current_user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Super Admins can perform this action.",
        )
    return current_user


def require_broker_or_admin(current_user: User = Depends(get_current_user)) -> User:
    """
    Dependency — Broker or Super Admin may proceed.
    Used on user-management endpoints.
    """
    if current_user.role not in (UserRole.BROKER, UserRole.SUPER_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Brokers or Super Admins can perform this action.",
        )
    return current_user


def require_active_user(current_user: User = Depends(get_current_user)) -> User:
    """Rejects suspended or non-active accounts."""
    from app.models.user import UserStatus
    if current_user.status not in (UserStatus.ACTIVE, UserStatus.PENDING_KYC):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account is suspended or inactive.",
        )
    return current_user


# ── Broker-scope ownership check ───────────────────────────────────────────

def assert_broker_owns_user(broker: User, target_user: User) -> None:
    """
    Per spec section 5.2:
    'Brokers can only manage their users.'
    Super Admin bypasses this check.
    """
    if broker.role == UserRole.SUPER_ADMIN:
        return   # Super Admin sees everything
    if target_user.broker_id != broker.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage this user.",
        )


# ── Leverage validation ────────────────────────────────────────────────────

def validate_leverage_caps(
    requester: User,
    equity : float | None,
    fno    : float | None,
    crypto : float | None,
    etf    : float | None,
    index  : float | None,
) -> None:
    """
    Ensure the requested leverage values do not exceed platform ceilings.
    Super Admin gets a multiplier of 2x for overrides.
    Raises HTTPException with a clear message if any cap is exceeded.
    """
    multiplier = 2.0 if requester.role == UserRole.SUPER_ADMIN else 1.0
    checks = [
        ("equity",  equity,  PLATFORM_MAX_LEVERAGE["equity"]  * multiplier),
        ("fno",     fno,     PLATFORM_MAX_LEVERAGE["fno"]     * multiplier),
        ("crypto",  crypto,  PLATFORM_MAX_LEVERAGE["crypto"]  * multiplier),
        ("etf",     etf,     PLATFORM_MAX_LEVERAGE["etf"]     * multiplier),
        ("index",   index,   PLATFORM_MAX_LEVERAGE["index"]   * multiplier),
    ]
    errors = []
    for product, value, ceiling in checks:
        if value is not None and value > ceiling:
            errors.append(
                f"{product}: requested {value}x exceeds platform ceiling of {ceiling}x"
            )
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"leverage_errors": errors},
        )
