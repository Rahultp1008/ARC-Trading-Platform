# app/core/security.py
# FIXED: create_access_token now safely extracts .value from enum roles
# so the JWT payload always contains a plain string like "user" / "super_admin"
# instead of enum repr. This ensures decode_access_token can do simple
# string comparison in dependencies.py.
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Password helpers ─────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── Access token ─────────────────────────────────────────────────────────

def create_access_token(user_id: int, role) -> str:
    """
    Create a short-lived JWT access token.
    'role' can be a UserRole enum or plain string — always stored as .value string.
    """
    role_value = role.value if hasattr(role, "value") else str(role).lower()
    payload = {
        "sub" : str(user_id),
        "role": role_value,
        "type": "access",
        "exp" : datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        ),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload if payload.get("type") == "access" else None
    except JWTError:
        return None


# ── Refresh token ────────────────────────────────────────────────────────

def create_refresh_token(user_id: int) -> str:
    """
    Create a long-lived JWT refresh token.
    Only stores user_id — no role to limit exposure.
    """
    payload = {
        "sub" : str(user_id),
        "type": "refresh",
        "exp" : datetime.now(timezone.utc) + timedelta(
            days=settings.REFRESH_TOKEN_EXPIRE_DAYS
        ),
    }
    return jwt.encode(payload, settings.REFRESH_SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_refresh_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(
            token, settings.REFRESH_SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        return payload if payload.get("type") == "refresh" else None
    except JWTError:
        return None
