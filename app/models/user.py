# app/models/user.py
# ---------------------------------------------------------------------------
# User model for the ARC Trading platform.
# Per spec section 5.2: Users are always scoped to a Broker via broker_id.
# Super Admin can see all; Brokers can only access their own users.
# ---------------------------------------------------------------------------
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean, DateTime, Enum, ForeignKey, Integer,
    Numeric, String, Text
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.broker import Broker
    from app.models.user_settings import UserSettings


class UserRole(str, enum.Enum):
    SUPER_ADMIN = "super_admin"
    BROKER      = "broker"
    USER        = "user"


class UserStatus(str, enum.Enum):
    ACTIVE             = "active"
    SUSPENDED          = "suspended"
    PENDING_KYC        = "pending_kyc"
    KYC_REJECTED       = "kyc_rejected"
    PENDING_ACTIVATION = "pending_activation"


class KYCStatus(str, enum.Enum):
    NOT_SUBMITTED         = "not_submitted"
    PENDING               = "pending"
    UNDER_REVIEW          = "under_review"
    APPROVED              = "approved"
    REJECTED              = "rejected"
    RESUBMISSION_REQUIRED = "resubmission_required"


class User(Base):
    __tablename__ = "users"

    id              : Mapped[int]        = mapped_column(Integer, primary_key=True, index=True)
    email           : Mapped[str]        = mapped_column(String(255), unique=True, nullable=False, index=True)
    full_name       : Mapped[str]        = mapped_column(String(255), nullable=False)
    phone           : Mapped[str | None] = mapped_column(String(20),  unique=True, nullable=True)
    hashed_password : Mapped[str]        = mapped_column(String(255), nullable=False)

    role      : Mapped[UserRole]   = mapped_column(Enum(UserRole),   default=UserRole.USER,               nullable=False)
    status    : Mapped[UserStatus] = mapped_column(Enum(UserStatus), default=UserStatus.PENDING_ACTIVATION, nullable=False)
    kyc_status: Mapped[KYCStatus]  = mapped_column(Enum(KYCStatus),  default=KYCStatus.NOT_SUBMITTED,     nullable=False)

    broker_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("brokers.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    broker: Mapped[Broker | None] = relationship("Broker", back_populates="users")

    max_leverage_equity : Mapped[float] = mapped_column(Numeric(6, 2), default=5.0,  nullable=False)
    max_leverage_fno    : Mapped[float] = mapped_column(Numeric(6, 2), default=2.0,  nullable=False)
    max_leverage_crypto : Mapped[float] = mapped_column(Numeric(6, 2), default=1.0,  nullable=False)
    max_leverage_etf    : Mapped[float] = mapped_column(Numeric(6, 2), default=3.0,  nullable=False)
    max_leverage_index  : Mapped[float] = mapped_column(Numeric(6, 2), default=2.0,  nullable=False)

    can_trade_equity: Mapped[bool] = mapped_column(Boolean, default=True,  nullable=False)
    can_trade_fno   : Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_trade_crypto: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_trade_etf   : Mapped[bool] = mapped_column(Boolean, default=True,  nullable=False)
    can_trade_index : Mapped[bool] = mapped_column(Boolean, default=True,  nullable=False)

    created_at   : Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at   : Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    last_login_at: Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes        : Mapped[str|None]      = mapped_column(Text, nullable=True)

    settings: Mapped[UserSettings | None] = relationship(
        "UserSettings", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email} role={self.role} status={self.status}>"
