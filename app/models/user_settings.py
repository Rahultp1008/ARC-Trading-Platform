# app/models/user_settings.py
# ---------------------------------------------------------------------------
# UserSettings — stores per-user preferences and risk profile details.
# Per spec section 5.2 data model: user_settings table.
# ---------------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.user import User


class UserSettings(Base):
    __tablename__ = "user_settings"

    id     : Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    # ── Notification preferences ───────────────────────────────────────────
    notify_order_filled  : Mapped[bool] = mapped_column(Boolean, default=True,  nullable=False)
    notify_order_rejected: Mapped[bool] = mapped_column(Boolean, default=True,  nullable=False)
    notify_sl_triggered  : Mapped[bool] = mapped_column(Boolean, default=True,  nullable=False)
    notify_funding       : Mapped[bool] = mapped_column(Boolean, default=True,  nullable=False)
    notify_kyc_update    : Mapped[bool] = mapped_column(Boolean, default=True,  nullable=False)

    # ── UI preferences ─────────────────────────────────────────────────────
    default_chart_interval: Mapped[str] = mapped_column(String(10), default="1D", nullable=False)
    theme                 : Mapped[str] = mapped_column(String(20), default="light", nullable=False)

    # ── Risk preference ────────────────────────────────────────────────────
    # User-chosen margin buffer (over and above platform minimum)
    margin_buffer_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=10.0, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user: Mapped[User] = relationship("User", back_populates="settings")

    def __repr__(self) -> str:
        return f"<UserSettings user_id={self.user_id}>"
