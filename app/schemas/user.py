# app/schemas/user.py
# ---------------------------------------------------------------------------
# Pydantic v2 schemas for User management endpoints.
# Used by both Broker (scoped) and Super Admin (global) endpoints.
# ---------------------------------------------------------------------------
from datetime import datetime
from pydantic import BaseModel, EmailStr, Field, field_validator, ConfigDict

from app.models.user import KYCStatus, UserRole, UserStatus


# ── Leverage profile sub-schema ────────────────────────────────────────────

class LeverageProfileRequest(BaseModel):
    """
    Per spec section 5.2 — Risk Profile Assignment.
    Broker sets per-user leverage caps within platform limits.
    Super Admin can override to any value.
    """
    max_leverage_equity: float | None = Field(default=None, ge=1.0, le=100.0)
    max_leverage_fno   : float | None = Field(default=None, ge=1.0, le=50.0)
    max_leverage_crypto: float | None = Field(default=None, ge=1.0, le=20.0)
    max_leverage_etf   : float | None = Field(default=None, ge=1.0, le=50.0)
    max_leverage_index : float | None = Field(default=None, ge=1.0, le=50.0)


class LeverageProfileResponse(BaseModel):
    max_leverage_equity: float
    max_leverage_fno   : float
    max_leverage_crypto: float
    max_leverage_etf   : float
    max_leverage_index : float
    model_config = ConfigDict(from_attributes=True)


# ── Product access sub-schema ──────────────────────────────────────────────

class ProductAccessRequest(BaseModel):
    """Which asset classes this user is allowed to trade."""
    can_trade_equity: bool | None = None
    can_trade_fno   : bool | None = None
    can_trade_crypto: bool | None = None
    can_trade_etf   : bool | None = None
    can_trade_index : bool | None = None


# ── Request bodies ─────────────────────────────────────────────────────────

class UserCreateRequest(BaseModel):
    """
    Broker creates a user under their scope.
    broker_id is injected from the authenticated broker's identity —
    the client never sends it directly.
    """
    email    : EmailStr
    full_name: str        = Field(min_length=2, max_length=255)
    password : str        = Field(min_length=8)
    phone    : str | None = Field(default=None, max_length=20)
    notes    : str | None = None

    # Optional initial leverage caps — broker can set at creation time
    leverage_profile: LeverageProfileRequest | None = None
    product_access  : ProductAccessRequest   | None = None

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter.")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit.")
        return v


class UserUpdateRequest(BaseModel):
    """
    Broker updates a user they own.
    Super Admin can update any user.
    """
    full_name: str | None = Field(default=None, min_length=2, max_length=255)
    phone    : str | None = Field(default=None, max_length=20)
    notes    : str | None = None


class UserStatusUpdateRequest(BaseModel):
    """Broker suspends/reactivates their user. Super Admin can do this for any user."""
    status: UserStatus
    reason: str = Field(min_length=5, max_length=500)


class UserLeverageUpdateRequest(BaseModel):
    """
    Assign or update leverage caps for a specific user.
    Per spec 5.2 — Risk Profile Assignment.
    """
    leverage_profile: LeverageProfileRequest
    product_access  : ProductAccessRequest | None = None


# ── Response bodies ────────────────────────────────────────────────────────

class UserResponse(BaseModel):
    """Full user profile — never exposes hashed_password."""
    id              : int
    email           : str
    full_name       : str
    phone           : str | None
    role            : UserRole
    status          : UserStatus
    kyc_status      : KYCStatus
    broker_id       : int | None
    notes           : str | None
    created_at      : datetime
    updated_at      : datetime
    last_login_at   : datetime | None

    # Leverage caps
    max_leverage_equity : float
    max_leverage_fno    : float
    max_leverage_crypto : float
    max_leverage_etf    : float
    max_leverage_index  : float

    # Product access
    can_trade_equity: bool
    can_trade_fno   : bool
    can_trade_crypto: bool
    can_trade_etf   : bool
    can_trade_index : bool

    model_config = ConfigDict(from_attributes=True)


class UserSummaryResponse(BaseModel):
    """Lightweight user card for list views."""
    id        : int
    email     : str
    full_name : str
    phone     : str | None
    role      : UserRole
    status    : UserStatus
    kyc_status: KYCStatus
    broker_id : int | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UserListResponse(BaseModel):
    total: int
    page : int
    size : int
    users: list[UserSummaryResponse]


class MessageResponse(BaseModel):
    message: str
