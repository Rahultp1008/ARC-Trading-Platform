# app/services/instruments/instrument_service.py
from __future__ import annotations

import logging
import uuid
from typing import Optional
from datetime import date

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.instruments import Instrument, InstrumentType
from app.schemas.instrument import (
    InstrumentSearch,
    InstrumentSearchResponse,
    InstrumentSummary,
    InstrumentToggleRequest,
    InstrumentToggleResponse,
)

logger = logging.getLogger(__name__)


class InstrumentService:

    def __init__(self, db: Session) -> None:
        self.db = db

    def search(self, params: InstrumentSearch) -> InstrumentSearchResponse:
        q = self.db.query(Instrument)

        if params.q:
            like = f"%{params.q}%"
            q = q.filter(
                or_(
                    Instrument.symbol.ilike(like),
                    Instrument.display_name.ilike(like),
                    Instrument.external_symbol.ilike(like),
                )
            )
        if params.instrument_type:
            q = q.filter(Instrument.instrument_type == params.instrument_type)
        if params.segment:
            q = q.filter(Instrument.segment == params.segment)
        if params.exchange:
            q = q.filter(Instrument.exchange == params.exchange)
        if params.is_active is not None:
            q = q.filter(Instrument.is_active == params.is_active)
        if params.trading_allowed is not None:
            q = q.filter(Instrument.trading_allowed == params.trading_allowed)

        total     = q.count()
        page      = params.page or 1
        page_size = params.page_size or 20
        offset    = (page - 1) * page_size
        instruments = q.offset(offset).limit(page_size).all()

        from app.schemas.instrument import InstrumentSearchResponse as _R
        fields = _R.model_fields.keys()
        response_data = dict(total=total, page=page)
        if "page_size" in fields:
            response_data["page_size"] = page_size
        if "size" in fields:
            response_data["size"] = page_size
        if "total_pages" in fields:
            response_data["total_pages"] = (
                (total + page_size - 1) // page_size if page_size else 0
            )
        summaries = [InstrumentSummary.model_validate(i) for i in instruments]
        if "items" in fields:
            response_data["items"] = summaries
        if "instruments" in fields:
            response_data["instruments"] = summaries

        return InstrumentSearchResponse(**response_data)

    def get_by_id(self, instrument_id: uuid.UUID) -> Optional[Instrument]:
        return self.db.get(Instrument, instrument_id)

    def toggle(
        self,
        instrument_id: uuid.UUID,
        payload: InstrumentToggleRequest,
        actor_id: int,
    ) -> Optional[InstrumentToggleResponse]:
        instrument = self.db.get(Instrument, instrument_id)
        if not instrument:
            return None

        if payload.is_active is not None:
            instrument.is_active = payload.is_active
        if payload.trading_allowed is not None:
            instrument.trading_allowed = payload.trading_allowed

        self.db.commit()
        self.db.refresh(instrument)

        from app.schemas.instrument import InstrumentToggleResponse as _TR
        fields = _TR.model_fields.keys()
        response_data = dict(
            id=instrument.id,
            symbol=instrument.symbol,
            is_active=instrument.is_active,
            trading_allowed=instrument.trading_allowed,
        )
        if "message" in fields:
            response_data["message"] = (
                f"Instrument {instrument.symbol} updated successfully."
            )
        return InstrumentToggleResponse(**response_data)

    def upsert_instruments(self, instruments: list[dict]) -> int:
        count = 0
        for data in instruments:
            existing = (
                self.db.query(Instrument)
                .filter(Instrument.symbol == data["symbol"])
                .first()
            )
            if existing:
                for k, v in data.items():
                    if hasattr(existing, k):
                        setattr(existing, k, v)
            else:
                instrument = Instrument(**{
                    k: v for k, v in data.items()
                    if hasattr(Instrument, k)
                })
                self.db.add(instrument)
            count += 1
        self.db.commit()
        return count

    def deactivate_expired(self) -> int:
        result = (
            self.db.query(Instrument)
            .filter(
                and_(
                    Instrument.expiry < date.today(),
                    Instrument.is_active == True,
                    Instrument.instrument_type.in_([
                        InstrumentType.FUTURES,
                        InstrumentType.OPTIONS,
                    ]),
                )
            )
            .all()
        )
        count = len(result)
        for inst in result:
            inst.is_active = False
        self.db.commit()
        return count