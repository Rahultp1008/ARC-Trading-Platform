# app/services/instruments/instrument_service.py
#
# FIXES APPLIED:
#   FIX 1: pagination now uses params.page_size correctly (was broken
#           because API passed size=size but schema field is page_size)
#   FIX 3: search() now enriches every result with live LTP, change,
#           change_pct from Redis price cache — search shows live prices
#   FIX 4: search adds upper() strip on q so "reliance" finds "RELIANCE"
from __future__ import annotations

import logging
import uuid
from typing import Optional
from datetime import date

from sqlalchemy import and_, or_, func
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


def _get_live_price(symbol: str) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """
    FIX 3: Fetch live LTP + change data from Redis price cache.
    Returns (ltp, change, change_pct, volume) — all None if simulator not running.

    Redis key: ltp:{symbol}  e.g. ltp:NSE:RELIANCE
    The price_cache.get_quote() returns the richer quote with change_pct.
    """
    try:
        from app.services.marketdata.price_cache import get_quote, get_ltp

        # Try full quote first (has change_pct)
        q = get_quote(symbol)
        if q and q.get("ltp"):
            return (
                float(q.get("ltp", 0) or 0),
                float(q.get("change", 0) or 0),
                float(q.get("change_pct", 0) or 0),
                float(q.get("volume", 0) or 0),
            )

        # Fall back to LTP-only cache
        ltp_data = get_ltp(symbol)
        if ltp_data and ltp_data.get("ltp"):
            return float(ltp_data["ltp"]), None, None, float(ltp_data.get("volume", 0) or 0)

    except Exception as exc:
        logger.debug("Price fetch skipped for %s: %s", symbol, exc)
    return None, None, None, None


class InstrumentService:

    def __init__(self, db: Session) -> None:
        self.db = db

    def search(self, params: InstrumentSearch) -> InstrumentSearchResponse:
        """
        FIX 1: Uses params.page_size (previously API was passing size= which
                is not a field in InstrumentSearch, so pagination was always 20)
        FIX 3: After fetching instrument rows, each result is enriched with
                live price data from Redis so the frontend shows live LTP
                in search results without an extra API call per instrument.
        FIX 4: Query is stripped and case-normalised before ILIKE search.
        """
        q = self.db.query(Instrument)

        # FIX 4: strip whitespace + case-insensitive search
        if params.q:
            search_term = params.q.strip()
            like = f"%{search_term}%"
            q = q.filter(
                or_(
                    Instrument.symbol.ilike(like),
                    Instrument.display_name.ilike(like),
                    Instrument.external_symbol.ilike(like),
                    # Also search on stripped/upper version for better matching
                    func.upper(Instrument.symbol).ilike(like.upper()),
                )
            )

        if params.instrument_type:
            q = q.filter(Instrument.instrument_type == params.instrument_type)
        if params.segment:
            q = q.filter(Instrument.segment == params.segment)
        if params.exchange:
            q = q.filter(Instrument.exchange.ilike(f"%{params.exchange}%"))
        if params.is_active is not None:
            q = q.filter(Instrument.is_active == params.is_active)
        if params.trading_allowed is not None:
            q = q.filter(Instrument.trading_allowed == params.trading_allowed)
        # F&O filters
        if params.underlying_symbol:
            q = q.filter(Instrument.underlying_symbol.ilike(f"%{params.underlying_symbol}%"))
        if params.option_type:
            q = q.filter(Instrument.option_type == params.option_type)
        if params.expiry_from:
            q = q.filter(Instrument.expiry >= params.expiry_from)
        if params.expiry_to:
            q = q.filter(Instrument.expiry <= params.expiry_to)
        if params.min_strike is not None:
            q = q.filter(Instrument.strike >= params.min_strike)
        if params.max_strike is not None:
            q = q.filter(Instrument.strike <= params.max_strike)

        # Sort: active + trading_allowed first, then alphabetical
        q = q.order_by(
            Instrument.is_active.desc(),
            Instrument.trading_allowed.desc(),
            Instrument.symbol.asc(),
        )

        total     = q.count()
        # FIX 1: use page_size (was broken — API was passing size= not page_size=)
        page      = params.page or 1
        page_size = params.page_size or 20
        offset    = (page - 1) * page_size
        instruments = q.offset(offset).limit(page_size).all()
        total_pages = (total + page_size - 1) // page_size if page_size else 0

        # FIX 3: Enrich each instrument with live price from Redis
        summaries = []
        for inst in instruments:
            # Build base summary from ORM object
            summary = InstrumentSummary.model_validate(inst)

            # Pull live price — won't crash if simulator is off
            ltp, change, change_pct, volume = _get_live_price(inst.symbol)
            summary.ltp        = ltp
            summary.change     = change
            summary.change_pct = change_pct
            summary.volume     = volume

            summaries.append(summary)

        return InstrumentSearchResponse(
            items       = summaries,
            total       = total,
            page        = page,
            page_size   = page_size,
            total_pages = total_pages,
        )

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

        return InstrumentToggleResponse(
            id              = instrument.id,
            symbol          = instrument.symbol,
            trading_allowed = instrument.trading_allowed,
            is_active       = instrument.is_active,
            message         = f"Instrument {instrument.symbol} updated successfully.",
        )

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
