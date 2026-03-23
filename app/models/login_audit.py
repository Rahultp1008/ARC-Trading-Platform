# app/models/login_audit.py
# ---------------------------------------------------------------------------
# Records every login attempt (successful or not).
# An audit log is critical for security: it helps you detect brute-force
# attacks, account takeovers, and unusual access patterns.
# ---------------------------------------------------------------------------


from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING        # ✅ FIX LINE 2

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


if TYPE_CHECKING:
    from app.models.user import User


class LoginAudit(Base):
    __tablename__ = "login_audit"

    id         : Mapped[int]      = mapped_column(Integer, primary_key=True, index=True)
    user_id    : Mapped[int]      = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    ip_address : Mapped[str]      = mapped_column(String(45), nullable=True)   # 45 chars covers IPv6
    user_agent : Mapped[str]      = mapped_column(String(512), nullable=True)
    success    : Mapped[bool]     = mapped_column(Boolean, nullable=False)
    created_at : Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


    user: Mapped[User] = relationship("User", back_populates="login_audits")

    def __repr__(self) -> str:
        return f"<LoginAudit id={self.id} user_id={self.user_id} success={self.success}>"