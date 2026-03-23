# app/api/v1/auth.py
# ---------------------------------------------------------------------------
# All authentication endpoints live here under the /api/v1/auth prefix.
# Each function is one HTTP route.  FastAPI calls our Depends() functions
# automatically before the route body runs.
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_role
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    hash_password,
    verify_password,
)
from app.models.login_audit import LoginAudit
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    RefreshRequest,
    TokenResponse,
    UserResponse,
)
from app.core.config import settings

router = APIRouter(prefix="/auth", tags=["Authentication"])


# ---------------------------------------------------------------------------
# Helper — persist a refresh token record in the DB
# ---------------------------------------------------------------------------
def _store_refresh_token(db: Session, user_id: int, token: str) -> None:
    """Save a newly issued refresh token so we can revoke it later."""
    record = RefreshToken(
        user_id   = user_id,
        token     = token,
        is_revoked= False,
        expires_at= datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(record)
    db.commit()


# ---------------------------------------------------------------------------
# POST /login
# ---------------------------------------------------------------------------
@router.post("/login", response_model=TokenResponse, status_code=status.HTTP_200_OK)
def login(
    payload : LoginRequest,
    request : Request,          # gives us IP + User-Agent for audit logging
    db      : Session = Depends(get_db),
):
    """
    Authenticate a user and issue a short-lived access token + long-lived
    refresh token.

    Steps:
      1. Look up the user by email.
      2. Verify the password against the stored bcrypt hash.
      3. Record the attempt in login_audit (success or failure).
      4. Issue tokens and save the refresh token in the DB.
    """
    user: User | None = db.query(User).filter(User.email == payload.email).first()

    # Helper to log the attempt regardless of outcome
    def audit(success: bool):
        if user:
            log = LoginAudit(
                user_id   = user.id,
                ip_address= request.client.host if request.client else None,
                user_agent= request.headers.get("user-agent"),
                success   = success,
            )
            db.add(log)
            db.commit()

    # Wrong email
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials.")

    # Wrong password
    if not verify_password(payload.password, user.hashed_password):
        audit(success=False)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials.")

    # Inactive account
    if not user.is_active:
        audit(success=False)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled.")

    # Everything is good — issue tokens
    audit(success=True)
    access_token  = create_access_token(user.id, user.role)
    refresh_token = create_refresh_token(user.id)
    _store_refresh_token(db, user.id, refresh_token)

    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


# ---------------------------------------------------------------------------
# POST /refresh  — Refresh Token Rotation
# ---------------------------------------------------------------------------
@router.post("/refresh", response_model=TokenResponse, status_code=status.HTTP_200_OK)
def refresh_tokens(payload: RefreshRequest, db: Session = Depends(get_db)):
    """
    Issue a new access + refresh token pair.

    Refresh Token Rotation explained:
      - Client sends its current refresh token.
      - We verify it is valid AND not already revoked.
      - We immediately mark it as revoked (so it can never be used again).
      - We issue a brand-new refresh token and save it.
      - If we ever see a *revoked* token being presented, it means someone
        still has the old token — possible token theft — so we revoke ALL
        tokens for that user and force re-login.
    """
    bad_request = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired refresh token.",
    )

    # 1. Decode the JWT to get the user_id
    token_data = decode_refresh_token(payload.refresh_token)
    if not token_data:
        raise bad_request

    user_id = int(token_data["sub"])

    # 2. Look up the token record in the DB
    stored: RefreshToken | None = (
        db.query(RefreshToken)
        .filter(
            RefreshToken.token    == payload.refresh_token,
            RefreshToken.user_id  == user_id,
        )
        .first()
    )

    if not stored:
        raise bad_request

    # 3. Token reuse detected — revoke everything for this user
    if stored.is_revoked:
        db.query(RefreshToken).filter(RefreshToken.user_id == user_id).update({"is_revoked": True})
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token reuse detected. All sessions have been revoked. Please log in again.",
        )

    # 4. Token has expired in the DB
    if stored.expires_at < datetime.now(timezone.utc):
        raise bad_request

    # 5. All good — rotate: revoke old, issue new
    stored.is_revoked = True
    db.commit()

    user: User | None = db.get(User, user_id)
    if not user or not user.is_active:
        raise bad_request

    new_access  = create_access_token(user.id, user.role)
    new_refresh = create_refresh_token(user.id)
    _store_refresh_token(db, user.id, new_refresh)

    return TokenResponse(access_token=new_access, refresh_token=new_refresh)


# ---------------------------------------------------------------------------
# POST /logout
# ---------------------------------------------------------------------------
@router.post("/logout", response_model=MessageResponse, status_code=status.HTTP_200_OK)
def logout(
    payload     : LogoutRequest,
    current_user: User    = Depends(get_current_user),
    db          : Session = Depends(get_db),
):
    """
    Revoke the provided refresh token.
    The client should also discard its access token locally (we cannot
    invalidate short-lived JWTs server-side without a blocklist, but they
    will expire in 15 minutes on their own).
    """
    stored: RefreshToken | None = (
        db.query(RefreshToken)
        .filter(
            RefreshToken.token   == payload.refresh_token,
            RefreshToken.user_id == current_user.id,
        )
        .first()
    )

    if stored and not stored.is_revoked:
        stored.is_revoked = True
        db.commit()

    return MessageResponse(message="Logged out successfully.")


# ---------------------------------------------------------------------------
# GET /me
# ---------------------------------------------------------------------------
@router.get("/me", response_model=UserResponse, status_code=status.HTTP_200_OK)
def get_me(current_user: User = Depends(get_current_user)):
    """
    Return the profile of the currently authenticated user.
    Protected by get_current_user dependency — 401 if no valid token.
    """
    return current_user


# ---------------------------------------------------------------------------
# POST /change-password
# ---------------------------------------------------------------------------
@router.post("/change-password", response_model=MessageResponse, status_code=status.HTTP_200_OK)
def change_password(
    payload     : ChangePasswordRequest,
    current_user: User    = Depends(get_current_user),
    db          : Session = Depends(get_db),
):
    """
    Securely update the user's password.

    Steps:
      1. Verify the *current* password so an attacker who hijacks a session
         cannot silently change the password without knowing the old one.
      2. Hash the new password with bcrypt.
      3. Revoke all existing refresh tokens to force re-login on all devices.
    """
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect.")

    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New password must differ from the current password.")

    # Update the password
    current_user.hashed_password = hash_password(payload.new_password)

    # Revoke all refresh tokens — forces re-login everywhere
    db.query(RefreshToken).filter(RefreshToken.user_id == current_user.id).update({"is_revoked": True})

    db.commit()

    return MessageResponse(message="Password updated successfully. Please log in again.")


# ---------------------------------------------------------------------------
# Example: a route only super_admin can access
# ---------------------------------------------------------------------------
@router.get(
    "/admin-dashboard",
    response_model=MessageResponse,
    dependencies=[Depends(require_role("super_admin"))],   # ← RBAC here
)
def admin_only_route():
    """
    Demonstrates RBAC. Only users with role `super_admin` can reach this.
    Brokers and regular users will receive a 403 Forbidden.
    """
    return MessageResponse(message="Welcome, Super Admin!")


# ---------------------------------------------------------------------------
# Example: a route for both super_admin and broker
# ---------------------------------------------------------------------------
@router.get(
    "/broker-area",
    response_model=MessageResponse,
    dependencies=[Depends(require_role("super_admin", "broker"))],
)
def broker_route():
    """Both super_admin and broker may access this endpoint."""
    return MessageResponse(message="Welcome to the broker area!")
