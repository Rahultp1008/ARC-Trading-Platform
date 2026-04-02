# app/schemas/auth.py
# ---------------------------------------------------------------------------
# Pydantic v2 schemas define the *shape* of request bodies and response
# payloads.  FastAPI validates incoming JSON against these automatically.
#
# Key Pydantic v2 change from v1: `model_config` replaces the inner `Config`
# class, and `model_validator` / `field_validator` replace `@validator`.
# ---------------------------------------------------------------------------

from pydantic import BaseModel, EmailStr, Field, field_validator


# ── Request bodies ──────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email   : EmailStr
    password: str = Field(min_length=1)


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password    : str = Field(min_length=8, description="Minimum 8 characters")

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        """Basic strength check — extend to your own policy."""
        if not any(c.isupper() for c in v):
            raise ValueError("New password must contain at least one uppercase letter.")
        if not any(c.isdigit() for c in v):
            raise ValueError("New password must contain at least one digit.")
        return v


# ── Response bodies ─────────────────────────────────────────────────────────

class TokenResponse(BaseModel):
    """Returned after a successful login or token refresh."""
    access_token : str
    refresh_token: str
    token_type   : str = "bearer"


class MessageResponse(BaseModel):
    """Generic success message response."""
    message: str


class UserResponse(BaseModel):
    """Public-safe user profile (never expose hashed_password!)."""
    id       : int
    email    : str
    role     : str
    is_active: bool

    # `model_config = {"from_attributes": True}` tells Pydantic to read
    # attributes from SQLAlchemy model instances (replaces `orm_mode = True`).
    model_config = {"from_attributes": True}
