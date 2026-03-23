# app/schemas/auth.py
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
    new_password    : str = Field(min_length=8)

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not any(c.isupper() for c in v):
            raise ValueError("New password must contain at least one uppercase letter.")
        if not any(c.isdigit() for c in v):
            raise ValueError("New password must contain at least one digit.")
        return v


# ── Response bodies ─────────────────────────────────────────────────────────

class TokenResponse(BaseModel):
    access_token : str
    refresh_token: str
    token_type   : str = "bearer"


class MessageResponse(BaseModel):
    message: str


class UserResponse(BaseModel):
    id       : int
    email    : str
    role     : str
    is_active: bool

    model_config = {"from_attributes": True}