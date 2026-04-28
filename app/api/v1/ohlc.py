"""
api/v1/ohlc.py — OHLC Candle APIs
Per spec 5.5: "OHLC caching and refresh"
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from app.core.dependencies import get_current_user
from app.services.marketdata import price_cache

router = APIRouter(prefix="/ohlc", tags=["OHLC Candles"])


@router.get("/{symbol}", summary="Get latest OHLC candle for a symbol")
def get_ohlc(
    symbol: str,
    interval: str = Query(default="1m", description="Candle interval: 1m, 5m, 15m, 1h, 1D"),
    _user=Depends(get_current_user),
):
    """Returns the latest OHLC candle for the given symbol and interval."""
    candle = price_cache.get_ohlc(symbol.upper(), interval)
    if not candle:
        raise HTTPException(status_code=404, detail=f"No OHLC data for {symbol} ({interval}). Is the simulator running?")
    return candle


@router.get("/{symbol}/series", summary="Get OHLC candle series for charting")
def get_ohlc_series(
    symbol: str,
    interval: str = Query(default="1m", description="1m, 5m, 15m, 1h, 1D"),
    _user=Depends(get_current_user),
):
    """Returns up to 30 recent candles for charting."""
    series = price_cache.get_ohlc_series(symbol.upper(), interval)
    return {"symbol": symbol.upper(), "interval": interval, "count": len(series), "candles": series}
