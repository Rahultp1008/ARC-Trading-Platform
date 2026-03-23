# app/core/dependencies.py
# ---------------------------------------------------------------------------
# Reusable FastAPI dependencies.
#
# A "dependency" is a function FastAPI calls automatically before your endpoint
# runs. If it raises an HTTPException the request is rejected immediately.
#
# Pattern:
#   @router.get("/admin-only")
#   def admin_route(current_user = Depends(require_role("super_admin"))):
#       ...
# ---------------------------------------------------------------------------

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.user import User

# HTTPBearer extracts the `Authorization: Bearer <token>` header automatically.
bearer_scheme = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    Dependency that:
      1. Reads the Bearer token from the Authorization header.
      2. Verifies and decodes the JWT.
      3. Loads the matching User from the DB.
      4. Raises 401 if any step fails.

    Inject this into any endpoint that requires the user to be logged in.
    """
    token = credentials.credentials
    payload = decode_access_token(token)

    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id: str = payload.get("sub")
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed token.")

    user = db.get(User, int(user_id))
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive.")

    return user


def require_role(*allowed_roles: str):
    """
    Factory that returns a dependency enforcing Role-Based Access Control (RBAC).

    Usage:
        # Only super_admin may call this endpoint
        @router.delete("/users/{id}", dependencies=[Depends(require_role("super_admin"))])

        # Both super_admin and broker may call this endpoint
        @router.get("/trades", dependencies=[Depends(require_role("super_admin", "broker"))])
    """
    def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required role(s): {', '.join(allowed_roles)}.",
            )
        return current_user

    return role_checker
