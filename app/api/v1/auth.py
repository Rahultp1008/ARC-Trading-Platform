# app/api/v1/auth.py
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.permission import require_super_admin, require_broker_or_admin
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    hash_password,
    verify_password,
)
from app.models.login_audit import LoginAudit
from app.models.refresh_token import RefreshToken
from app.models.user import User, UserStatus
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


def _store_refresh_token(db: Session, user_id: int, token: str) -> None:
    # FIXED: guard against duplicate token inserts (unique constraint violation)
    existing = db.query(RefreshToken).filter(RefreshToken.token == token).first()
    if existing:
        return
    record = RefreshToken(
        user_id    = user_id,
        token      = token,
        is_revoked = False,
        expires_at = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(record)
    db.commit()


@router.post("/login", response_model=TokenResponse, status_code=status.HTTP_200_OK)
def login(
    payload: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Authenticate a user and issue a short-lived access token + long-lived
    refresh token.
    """
    user: User | None = db.query(User).filter(User.email == payload.email).first()

    def audit(success: bool):
        if user:
            db.add(LoginAudit(
                user_id    = user.id,
                ip_address = request.client.host if request.client else None,
                user_agent = request.headers.get("user-agent"),
                success    = success,
            ))
            db.commit()

    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials.")

    if not verify_password(payload.password, user.hashed_password):
        audit(success=False)
        raise HTTPException(status_code=401, detail="Invalid credentials.")

    if user.status not in (UserStatus.ACTIVE, UserStatus.PENDING_KYC):
        audit(success=False)
        raise HTTPException(status_code=403, detail="Account is disabled.")

    audit(success=True)
    access_token  = create_access_token(user.id, user.role)
    refresh_token = create_refresh_token(user.id)
    _store_refresh_token(db, user.id, refresh_token)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()

    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenResponse, status_code=status.HTTP_200_OK)
def refresh_tokens(payload: RefreshRequest, db: Session = Depends(get_db)):
    """
    Issue a new access + refresh token pair (Refresh Token Rotation).
    Presenting a revoked token revokes ALL sessions for that user.
    """
    bad = HTTPException(status_code=401, detail="Invalid or expired refresh token.")

    token_data = decode_refresh_token(payload.refresh_token)
    if not token_data:
        raise bad

    user_id = int(token_data["sub"])
    stored = db.query(RefreshToken).filter(
        RefreshToken.token   == payload.refresh_token,
        RefreshToken.user_id == user_id,
    ).first()

    if not stored:
        raise bad

    if stored.is_revoked:
        db.query(RefreshToken).filter(RefreshToken.user_id == user_id).update({"is_revoked": True})
        db.commit()
        raise HTTPException(status_code=401, detail="Token reuse detected. Please log in again.")

    # FIXED: safely handle both timezone-aware and naive datetimes
    exp = stored.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    else:
        exp = exp.astimezone(timezone.utc)
    if exp < datetime.now(timezone.utc):
        raise bad

    stored.is_revoked = True
    db.commit()

    user = db.get(User, user_id)
    if not user or user.status not in (UserStatus.ACTIVE, UserStatus.PENDING_KYC):
        raise bad

    new_access  = create_access_token(user.id, user.role)
    new_refresh = create_refresh_token(user.id)
    _store_refresh_token(db, user.id, new_refresh)

    return TokenResponse(access_token=new_access, refresh_token=new_refresh)


@router.post("/logout", response_model=MessageResponse, status_code=status.HTTP_200_OK)
def logout(
    payload     : LogoutRequest,
    current_user: User    = Depends(get_current_user),
    db          : Session = Depends(get_db),
):
    """Revoke the provided refresh token."""
    stored = db.query(RefreshToken).filter(
        RefreshToken.token   == payload.refresh_token,
        RefreshToken.user_id == current_user.id,
    ).first()
    if stored and not stored.is_revoked:
        stored.is_revoked = True
        db.commit()
    return MessageResponse(message="Logged out successfully.")


@router.get("/me", response_model=UserResponse, status_code=status.HTTP_200_OK)
def get_me(current_user: User = Depends(get_current_user)):
    """Return the profile of the currently authenticated user."""
    return current_user


@router.post("/change-password", response_model=MessageResponse, status_code=status.HTTP_200_OK)
def change_password(
    payload     : ChangePasswordRequest,
    current_user: User    = Depends(get_current_user),
    db          : Session = Depends(get_db),
):
    """Securely update the user's password and revoke all existing sessions."""
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=400, detail="New password must differ from current.")
    current_user.hashed_password = hash_password(payload.new_password)
    db.query(RefreshToken).filter(RefreshToken.user_id == current_user.id).update({"is_revoked": True})
    db.commit()
    return MessageResponse(message="Password updated. Please log in again.")


@router.get("/admin-dashboard", response_model=MessageResponse, dependencies=[Depends(require_super_admin)])
def admin_only_route():
    """Super Admin only endpoint."""
    return MessageResponse(message="Welcome, Super Admin!")


@router.get("/broker-area", response_model=MessageResponse, dependencies=[Depends(require_broker_or_admin)])
def broker_route():
    """Super Admin and Broker endpoint."""
    return MessageResponse(message="Welcome to the broker area!")