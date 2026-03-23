# app/core/security.py
# ---------------------------------------------------------------------------
# All cryptographic helpers live here so the rest of the app never has to
# know *how* passwords are hashed or tokens are signed.
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

# ---------------------------------------------------------------------------
# Password hashing with bcrypt
# ---------------------------------------------------------------------------
# CryptContext handles the algorithm details for us.
# bcrypt is deliberately slow to make brute-force attacks expensive.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    """Return a bcrypt hash of the plain-text password."""
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Return True if the plain-text password matches the stored hash.
    bcrypt's constant-time comparison prevents timing attacks.
    """
    return pwd_context.verify(plain_password, hashed_password)


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------
def _create_token(
    data: dict,
    secret: str,
    expires_delta: timedelta,
) -> str:
    """
    Internal helper — stamps an expiry claim onto `data` and signs it.
    We use timezone-aware UTC datetimes to avoid ambiguity.
    """
    payload = data.copy()
    expire = datetime.now(timezone.utc) + expires_delta
    payload.update({"exp": expire})
    return jwt.encode(payload, secret, algorithm=settings.ALGORITHM)


def create_access_token(user_id: int, role: str) -> str:
    """
    Short-lived access token (default 15 min).
    Embeds user_id and role so we can authorise without a DB hit on every request.
    """
    return _create_token(
        data={"sub": str(user_id), "role": role, "type": "access"},
        secret=settings.SECRET_KEY,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )


def create_refresh_token(user_id: int) -> str:
    """
    Long-lived refresh token (default 7 days).
    Uses a *different* secret so a leaked access token cannot forge refreshes.
    """
    return _create_token(
        data={"sub": str(user_id), "type": "refresh"},
        secret=settings.REFRESH_SECRET_KEY,
        expires_delta=timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )


def decode_access_token(token: str) -> Optional[dict]:
    """
    Decode and verify an access token.
    Returns the payload dict, or None if invalid/expired.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        # Extra guard: make sure this is actually an access token
        if payload.get("type") != "access":
            return None
        return payload
    except JWTError:
        return None


def decode_refresh_token(token: str) -> Optional[dict]:
    """
    Decode and verify a refresh token.
    Returns the payload dict, or None if invalid/expired.
    """
    try:
        payload = jwt.decode(token, settings.REFRESH_SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != "refresh":
            return None
        return payload
    except JWTError:
        return None
