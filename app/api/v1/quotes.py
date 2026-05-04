# app/api/v1/quotes.py
from fastapi import APIRouter, Depends, HTTPException, Query
from app.core.dependencies import get_current_user
from app.services.marketdata.price_cache import (
    get_quote, get_ltp, get_all_ltps
)

router = APIRouter(prefix="/quotes", tags=["Market Data — Quotes"])


@router.get(
    "",
    summary="Get all cached LTPs",
    description="Returns all live prices. Filter by ?segment=CASH|FNO|CRYPTO",
)
def get_all_quotes(
    segment: str | None = Query(None, description="CASH, FNO, or CRYPTO"),
    _user=Depends(get_current_user),
):
    ltps = get_all_ltps()

    if segment:
        seg = segment.upper()
        prefix_map = {"CASH": "NSE:", "FNO": "NFO:", "CRYPTO": "CRYPTO:"}
        prefix = prefix_map.get(seg, seg + ":")
        ltps = [l for l in ltps if l.get("symbol", "").startswith(prefix)]

    return {"count": len(ltps), "prices": ltps}


@router.get(
    "/{symbol}/ltp",
    summary="Get LTP only for a symbol (fastest path)",
)
def get_ltp_only(
    symbol: str,
    _user=Depends(get_current_user),
):
    ltp = get_ltp(symbol.upper())
    if not ltp:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No LTP found for '{symbol}'. "
                "Start the price simulator first: POST /api/v1/market/simulator/start"
            ),
        )
    return ltp


@router.get(
    "/{symbol}",
    summary="Get latest full quote for a symbol",
)
def get_quote_endpoint(
    symbol: str,
    _user=Depends(get_current_user),
):
    """
    Returns full quote: ltp, bid, ask, open, high, low, close,
    volume, change, change_pct, is_stale, source, timestamp.

    Symbol format: NSE:RELIANCE  or  CRYPTO:BTCUSDT
    """
    quote = get_quote(symbol.upper())
    if not quote:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No quote found for '{symbol}'. "
                "Start the price simulator: POST /api/v1/market/simulator/start"
            ),
        )
    return quote
