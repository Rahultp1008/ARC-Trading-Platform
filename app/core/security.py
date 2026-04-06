# app/core/security.py
# ---------------------------------------------------------------------------
# Password hashing and JWT token utilities.
#
# TWO SEPARATE TOKEN TYPES:
#   Access token  — short-lived (15 min), signed with SECRET_KEY
#                   sent in every API request Authorization header
#   Refresh token — long-lived (7 days), signed with REFRESH_SECRET_KEY
#                   stored in the DB, used only to get new access tokens
#
# Using a SEPARATE secret key for refresh tokens means a leaked access
# token cannot be used to forge a refresh token, and vice versa.
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

# bcrypt is the industry standard for password hashing.
# deprecated="auto" means passlib will automatically re-hash old bcrypt
# variants to the current recommended settings on next login.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Password helpers ──────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    """Return a bcrypt hash of the plaintext password."""
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if the plaintext matches the stored bcrypt hash."""
    return pwd_context.verify(plain, hashed)


# ── Access token ──────────────────────────────────────────────────────────

def create_access_token(user_id: int, role: str) -> str:
    """
    Create a short-lived JWT access token (default: 15 minutes).
    Signed with SECRET_KEY. Carries user_id (sub), role, and type="access".
    """
    payload = {
        "sub" : str(user_id),
        "role": role,
        "type": "access",
        "exp" : datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        ),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    """
    Decode and validate an access token.
    Returns the payload dict, or None if invalid/expired/wrong type.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload if payload.get("type") == "access" else None
    except JWTError:
        return None


# ── Refresh token ─────────────────────────────────────────────────────────

def create_refresh_token(user_id: int) -> str:
    """
    Create a long-lived JWT refresh token (default: 7 days).
    Signed with REFRESH_SECRET_KEY (different from access token secret).
    Carries only user_id and type="refresh" — no role, to limit exposure.
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
    """
    Decode and validate a refresh token.
    Uses REFRESH_SECRET_KEY — will reject any access token presented here.
    Returns the payload dict, or None if invalid/expired/wrong type.
    """
    try:
        payload = jwt.decode(
            token, settings.REFRESH_SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        return payload if payload.get("type") == "refresh" else None
    except JWTError:
        return None