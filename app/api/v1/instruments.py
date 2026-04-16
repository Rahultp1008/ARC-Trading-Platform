# app/api/v1/instruments.py
# FIXED:
#   1. Converted from async (AsyncSession) → sync (Session) to match the
#      synchronous database engine used throughout the project.
#   2. Removed CurrentUser dataclass dependency — now uses full User ORM
#      object returned by the fixed get_current_user / require_role.
#   3. Imports corrected (app.models.instruments for PriceSource).
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database     import get_db
from app.core.dependencies import get_current_user, require_role
from app.core.config       import settings
from app.integrations.binance.adapter           import BinanceAdapter
from app.integrations.kite.adapter              import KiteAdapter
from app.models.instruments                     import PriceSource
from app.models.user                            import User
from app.schemas.instrument import (
    InstrumentRead,
    InstrumentSearch,
    InstrumentSearchResponse,
    InstrumentSyncResult,
    InstrumentToggleRequest,
    InstrumentToggleResponse,
)
from app.services.instruments.instrument_service import InstrumentService
from app.services.instruments.sync_service       import InstrumentSyncService

router = APIRouter(prefix="/instruments", tags=["Instruments"])


def _svc(db: Session = Depends(get_db)) -> InstrumentService:
    return InstrumentService(db)


def _sync_svc(db: Session = Depends(get_db)) -> InstrumentSyncService:
    return InstrumentSyncService(
        db=db,
        kite_adapter=KiteAdapter(
            api_key=settings.KITE_API_KEY or "",
            access_token=settings.KITE_ACCESS_TOKEN or "",
        ),
        binance_adapter=BinanceAdapter(
            api_key=settings.BINANCE_API_KEY,
        ),
    )


# ── GET /instruments — Search ──────────────────────────────────────────────

@router.get(
    "",
    response_model=InstrumentSearchResponse,
    summary="Search instruments (all authenticated users)",
)
def search_instruments(
    q              : str | None = Query(None, description="Search by symbol or name"),
    instrument_type: str | None = Query(None),
    segment        : str | None = Query(None),
    exchange       : str | None = Query(None),
    is_active      : bool | None = Query(None),
    trading_allowed: bool | None = Query(None),
    page           : int = Query(1, ge=1),
    size           : int = Query(20, ge=1, le=100),
    db             : Session = Depends(get_db),
    _              : User = Depends(get_current_user),
):
    """
    Search/list instruments. Available to every authenticated user.
    Supports filter by type, segment, exchange, active/trading flags.
    """
    from app.models.instruments import InstrumentType, Segment

    params = InstrumentSearch(
        q=q,
        instrument_type=InstrumentType(instrument_type) if instrument_type else None,
        segment=Segment(segment) if segment else None,
        exchange=exchange,
        is_active=is_active,
        trading_allowed=trading_allowed,
        page=page,
        size=size,
    )
    svc = InstrumentService(db)
    return svc.search(params)


# ── GET /instruments/{id} — Detail ────────────────────────────────────────

@router.get(
    "/{instrument_id}",
    response_model=InstrumentRead,
    summary="Get full instrument detail",
)
def get_instrument(
    instrument_id: uuid.UUID,
    db           : Session = Depends(get_db),
    _            : User = Depends(get_current_user),
):
    svc = InstrumentService(db)
    instrument = svc.get_by_id(instrument_id)
    if not instrument:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instrument {instrument_id} not found.",
        )
    return instrument


# ── PATCH /instruments/{id}/toggle — Enable/disable ───────────────────────

@router.patch(
    "/{instrument_id}/toggle",
    response_model=InstrumentToggleResponse,
    summary="Enable or disable an instrument (Super Admin only)",
)
def toggle_instrument(
    instrument_id: uuid.UUID,
    payload      : InstrumentToggleRequest,
    db           : Session = Depends(get_db),
    actor        : User = Depends(require_role("super_admin")),
):
    svc = InstrumentService(db)
    result = svc.toggle(instrument_id, payload, actor_id=actor.id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instrument {instrument_id} not found.",
        )
    return result


# ── POST /instruments/sync — Trigger sync ─────────────────────────────────

@router.post(
    "/sync",
    response_model=InstrumentSyncResult,
    summary="Trigger instrument sync from Kite/Binance (Super Admin only)",
)
def trigger_sync(
    source: str = Query("all", description="'kite', 'binance', or 'all'"),
    db    : Session = Depends(get_db),
    actor : User = Depends(require_role("super_admin")),
):
    """
    Pulls fresh instrument data from Kite Connect and/or Binance,
    upserts into the instruments table, and deactivates expired F&O contracts.

    Limited to 100 instruments total per the platform configuration:
      - 20 top NSE equity stocks
      - 20 top crypto pairs (USDT)
      - 30 Nifty 50 index constituents
      - 30 top ETFs
    """
    sync_svc = InstrumentSyncService(
        db=db,
        kite_adapter=KiteAdapter(
            api_key=settings.KITE_API_KEY or "",
            access_token=settings.KITE_ACCESS_TOKEN or "",
        ),
        binance_adapter=BinanceAdapter(api_key=settings.BINANCE_API_KEY),
    )
    try:
        if source == "kite":
            return sync_svc.sync_kite()
        elif source == "binance":
            return sync_svc.sync_binance()
        else:
            return sync_svc.sync_all()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Sync failed: {exc}",
        )
