# app/models/broker.py
# ---------------------------------------------------------------------------
# Broker model — represents an intermediary who manages a set of users.
# Per spec section 3.2: Broker creates/manages users, handles KYC, funding,
# and configures leverage within allowed platform bounds.
# ---------------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.user import User


class Broker(Base):
    __tablename__ = "brokers"

    id          : Mapped[int]  = mapped_column(Integer, primary_key=True, index=True)
    email       : Mapped[str]  = mapped_column(String(255), unique=True, nullable=False, index=True)
    full_name   : Mapped[str]  = mapped_column(String(255), nullable=False)
    phone       : Mapped[str | None] = mapped_column(String(20),  unique=True, nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Broker's own hashed password (brokers also log in)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    # is_active=False means the broker is suspended by Super Admin
    is_active   : Mapped[bool] = mapped_column(Boolean, default=True,  nullable=False)
    is_verified : Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Maximum number of users this broker can onboard (0 = unlimited)
    max_users   : Mapped[int]  = mapped_column(Integer, default=0, nullable=False)

    # Notes set by Super Admin
    admin_notes : Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at  : Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at  : Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # One broker owns many users (back-reference set in User model)
    users: Mapped[list[User]] = relationship(
        "User", back_populates="broker", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Broker id={self.id} email={self.email} active={self.is_active}>"
