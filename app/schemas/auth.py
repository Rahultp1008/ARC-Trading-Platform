# app/schemas/auth.py
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict


class LoginRequest(BaseModel):
    email   : str
    password: str


class TokenResponse(BaseModel):
    access_token : str
    refresh_token: str
    token_type   : str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password    : str


class MessageResponse(BaseModel):
    message: str


class UserResponse(BaseModel):
    """Matches the actual User model fields — no is_active (use status instead)."""
    id           : int
    email        : str
    full_name    : str
    phone        : Optional[str]      = None
    role         : str
    status       : str
    kyc_status   : str
    broker_id    : Optional[int]      = None
    created_at   : datetime
    last_login_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
