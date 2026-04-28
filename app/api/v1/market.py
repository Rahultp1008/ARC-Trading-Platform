"""
api/v1/market.py — Market Status & Simulator Control APIs
Per spec 5.5: "Market session awareness"
"""
from fastapi import APIRouter, Depends, HTTPException
from app.core.dependencies import get_current_user, require_role
from app.services.marketdata import price_cache
from app.services.marketdata import price_simulator

router = APIRouter(prefix="/market", tags=["Market Status & Simulator"])


@router.get("/status", summary="Get market status for all exchanges")
def get_market_status(_user=Depends(get_current_user)):
    """
    Returns open/closed status for NSE, NFO, BINANCE.
    Per spec: "Market session awareness"
    """
    statuses = price_cache.get_all_market_statuses()
    if not statuses:
        return {
            "statuses": [
                {"exchange": "NSE", "status": "unknown", "updated_at": None},
                {"exchange": "NFO", "status": "unknown", "updated_at": None},
                {"exchange": "BINANCE", "status": "unknown", "updated_at": None},
            ],
            "note": "Start the price simulator to get live statuses"
        }
    return {"statuses": statuses}


@router.get("/status/{exchange}", summary="Get market status for one exchange")
def get_exchange_status(exchange: str, _user=Depends(get_current_user)):
    status = price_cache.get_market_status(exchange.upper())
    if not status:
        raise HTTPException(status_code=404, detail=f"No status for {exchange}")
    return status


# ── Simulator Controls (Super Admin only) ─────────────────────────────────

@router.post("/simulator/start", summary="[Admin] Start the price simulator")
def start_simulator(_admin=Depends(require_role("SUPER_ADMIN"))):
    """
    Starts background price simulation for all 140+ instruments.
    Ticks every 2 seconds with realistic random walk.
    Use this for development/testing without real Kite/Binance APIs.
    """
    started = price_simulator.start()
    if not started:
        return {"status": "already_running", "message": "Simulator is already running"}
    return {"status": "started", "message": "Price simulator started — ticking every 2s"}


@router.post("/simulator/stop", summary="[Admin] Stop the price simulator")
def stop_simulator(_admin=Depends(require_role("SUPER_ADMIN"))):
    price_simulator.stop()
    return {"status": "stopped", "message": "Price simulator stopped"}


@router.get("/simulator/status", summary="Check if simulator is running")
def simulator_status(_user=Depends(get_current_user)):
    return {
        "running": price_simulator.is_running(),
        "instruments_count": len(price_simulator.get_all_current_prices()),
    }
