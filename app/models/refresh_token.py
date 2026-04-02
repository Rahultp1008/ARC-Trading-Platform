# app/models/refresh_token.py
# ---------------------------------------------------------------------------
# Stores refresh tokens in the database so we can:
#   1. Revoke them on logout.
#   2. Detect token reuse (a security signal for token theft).
#
# Refresh Token Rotation means: every time a refresh token is used, we
# immediately revoke it and issue a *brand-new* refresh token.  If the old
# token is presented again it means someone (possibly an attacker) still has
# it — we can then revoke ALL tokens for that user.
# ---------------------------------------------------------------------------

# Same fix as user.py — makes all annotations lazy strings so SQLAlchemy
# resolves "User" from its mapper registry instead of crashing with NameError.
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

# Only imported during static type-checking (mypy/pyright), not at runtime.
# This breaks the potential circular import: refresh_token → user → refresh_token.
if TYPE_CHECKING:
    from app.models.user import User


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id         : Mapped[int]      = mapped_column(Integer, primary_key=True, index=True)
    user_id    : Mapped[int]      = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token      : Mapped[str]      = mapped_column(Text, unique=True, nullable=False)
    is_revoked : Mapped[bool]     = mapped_column(Boolean, default=False, nullable=False)
    created_at : Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at : Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Relationship back to the User.
    # "User" (string) is resolved by SQLAlchemy's mapper registry at startup.
    user: Mapped[User] = relationship("User", back_populates="refresh_tokens")

    def __repr__(self) -> str:
        return f"<RefreshToken id={self.id} user_id={self.user_id} revoked={self.is_revoked}>"
