# app/api/v1/auth.py
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
from app.models.broker import Broker
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


# ---------------------------------------------------------------------------
# Helper — persist a refresh token record in the DB
# ---------------------------------------------------------------------------
def _store_refresh_token(db: Session, user_id: int, token: str) -> None:
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
    payload: LoginRequest,
    request: Request,
    db     : Session = Depends(get_db),
):
    # ── 1. CHECK USERS TABLE ──
    user = db.query(User).filter(User.email == payload.email).first()

    if user:
        if not verify_password(payload.password, user.hashed_password):
            # Log failed attempt
            db.add(LoginAudit(
                user_id   = user.id,
                ip_address= request.client.host if request.client else None,
                user_agent= request.headers.get("user-agent"),
                success   = False,
            ))
            db.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Invalid credentials.")

        if user.status == UserStatus.SUSPENDED:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Account is suspended.")

        # Log success
        db.add(LoginAudit(
            user_id   = user.id,
            ip_address= request.client.host if request.client else None,
            user_agent= request.headers.get("user-agent"),
            success   = True,
        ))
        user.last_login_at = datetime.now(timezone.utc)

        access_token  = create_access_token(user.id, user.role)
        refresh_token = create_refresh_token(user.id)   # FIX: int, not dict
        _store_refresh_token(db, user.id, refresh_token)

        return TokenResponse(
            access_token =access_token,
            refresh_token=refresh_token,
            token_type   ="bearer",
        )

    # ── 2. CHECK BROKERS TABLE ──
    broker = db.query(Broker).filter(Broker.email == payload.email).first()

    if broker:
        if not verify_password(payload.password, broker.hashed_password):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Invalid credentials.")

        if not broker.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Broker account is suspended.")

        access_token  = create_access_token(broker.id, "broker")
        refresh_token = create_refresh_token(broker.id)   # FIX: int, not dict
        _store_refresh_token(db, broker.id, refresh_token)

        return TokenResponse(
            access_token =access_token,
            refresh_token=refresh_token,
            token_type   ="bearer",
        )

    # ── 3. NOT FOUND ──
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Invalid credentials.")


# ---------------------------------------------------------------------------
# POST /refresh  — Refresh Token Rotation
# ---------------------------------------------------------------------------
@router.post("/refresh", response_model=TokenResponse, status_code=status.HTTP_200_OK)
def refresh_tokens(payload: RefreshRequest, db: Session = Depends(get_db)):
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
            RefreshToken.token   == payload.refresh_token,
            RefreshToken.user_id == user_id,
        )
        .first()
    )
    if not stored:
        raise bad_request

    # 3. Token reuse detected — revoke ALL tokens for this user
    if stored.is_revoked:
        db.query(RefreshToken).filter(
            RefreshToken.user_id == user_id
        ).update({"is_revoked": True})
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token reuse detected. All sessions revoked. Please log in again.",
        )

    # 4. Token expired in DB
    if stored.expires_at < datetime.now(timezone.utc):
        raise bad_request

    # 5. Rotate — revoke old, issue new
    stored.is_revoked = True
    db.commit()

    user: User | None = db.get(User, user_id)
    if not user or user.status == UserStatus.SUSPENDED:
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
@router.get("/me")
def get_me(current_user=Depends(get_current_user)):
    return {
        "id"       : current_user.id,
        "email"    : current_user.email,
        "full_name": getattr(current_user, "full_name", None),
        "role"     : str(current_user.role.value if hasattr(current_user.role, "value") else current_user.role),
        "status"   : str(getattr(current_user, "status", "active")),
    }


# ---------------------------------------------------------------------------
# POST /change-password
# ---------------------------------------------------------------------------
@router.post("/change-password", response_model=MessageResponse, status_code=status.HTTP_200_OK)
def change_password(
    payload     : ChangePasswordRequest,
    current_user: User    = Depends(get_current_user),
    db          : Session = Depends(get_db),
):
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Current password is incorrect.")

    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="New password must differ from the current password.")

    current_user.hashed_password = hash_password(payload.new_password)

    # Revoke all refresh tokens — forces re-login on all devices
    db.query(RefreshToken).filter(
        RefreshToken.user_id == current_user.id
    ).update({"is_revoked": True})

    db.commit()
    return MessageResponse(message="Password updated successfully. Please log in again.")


# ---------------------------------------------------------------------------
# Demo RBAC routes
# ---------------------------------------------------------------------------
@router.get(
    "/admin-dashboard",
    response_model=MessageResponse,
    dependencies=[Depends(require_role("super_admin"))],
)
def admin_only_route():
    return MessageResponse(message="Welcome, Super Admin!")


@router.get(
    "/broker-area",
    response_model=MessageResponse,
    dependencies=[Depends(require_role("super_admin", "broker"))],
)
def broker_route():
    return MessageResponse(message="Welcome to the broker area!")