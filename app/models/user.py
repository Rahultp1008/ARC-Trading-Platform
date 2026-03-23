# app/models/user.py
# ---------------------------------------------------------------------------
# SQLAlchemy ORM model for the `users` table.
# Each Python class attribute maps to a database column.
# ---------------------------------------------------------------------------


# Without this, Python tries to resolve "RefreshToken" and "LoginAudit"
# immediately when the class is being defined — but those classes live in
# other files and are not in scope yet, causing a NameError.
# With this line, ALL annotations in this file become lazy strings.
# SQLAlchemy resolves them AFTER all models are fully loaded.
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING  # ✅ FIX LINE 2

from sqlalchemy import Boolean, DateTime, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


# These imports NEVER execute when your app runs — zero circular import risk.
# They exist ONLY so your IDE and type checker understand the types.
if TYPE_CHECKING:
    from app.models.refresh_token import RefreshToken
    from app.models.login_audit import LoginAudit


class UserRole(str, enum.Enum):
    """
    Using a Python Enum for the role keeps the set of valid values explicit
    and lets PostgreSQL enforce it at the DB level too (via a CHECK constraint).
    """
    SUPER_ADMIN = "super_admin"
    BROKER      = "broker"
    USER        = "user"


class User(Base):
    __tablename__ = "users"

    id             : Mapped[int]      = mapped_column(Integer, primary_key=True, index=True)
    email          : Mapped[str]      = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str]      = mapped_column(String(255), nullable=False)
    role           : Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.USER, nullable=False)
    is_active      : Mapped[bool]     = mapped_column(Boolean, default=True, nullable=False)
    created_at     : Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    
    # from __future__ import annotations makes "RefreshToken" and "LoginAudit"
    # lazy strings. SQLAlchemy looks them up in its mapper registry at startup,
    # AFTER all model files have been imported via app/models/__init__.py
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )
    login_audits: Mapped[list[LoginAudit]] = relationship(
        "LoginAudit", back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email} role={self.role}>"
