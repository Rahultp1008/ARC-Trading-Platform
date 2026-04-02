# app/schemas/broker.py
from datetime import datetime
from pydantic import BaseModel, EmailStr, Field, field_validator, ConfigDict


class BrokerCreateRequest(BaseModel):
    email       : EmailStr
    full_name   : str        = Field(min_length=2, max_length=255)
    password    : str        = Field(min_length=8)
    phone       : str | None = Field(default=None, max_length=20)
    company_name: str | None = Field(default=None, max_length=255)
    max_users   : int        = Field(default=0, ge=0)
    admin_notes : str | None = None

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not any(c.isupper() for c in v): raise ValueError("Needs uppercase.")
        if not any(c.isdigit() for c in v): raise ValueError("Needs digit.")
        return v


class BrokerUpdateRequest(BaseModel):
    full_name   : str | None  = Field(default=None, min_length=2, max_length=255)
    phone       : str | None  = Field(default=None, max_length=20)
    company_name: str | None  = None
    is_active   : bool | None = None
    is_verified : bool | None = None
    max_users   : int | None  = Field(default=None, ge=0)
    admin_notes : str | None  = None


class BrokerSuspendRequest(BaseModel):
    reason: str = Field(min_length=5, max_length=500)


class BrokerResponse(BaseModel):
    id          : int
    email       : str
    full_name   : str
    phone       : str | None
    company_name: str | None
    is_active   : bool
    is_verified : bool
    max_users   : int
    admin_notes : str | None
    user_count  : int = 0
    created_at  : datetime
    updated_at  : datetime
    model_config = ConfigDict(from_attributes=True)


class BrokerListResponse(BaseModel):
    total  : int
    page   : int
    size   : int
    brokers: list[BrokerResponse]
