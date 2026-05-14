# app/main.py
# ─────────────────────────────────────────────────────────────────────────
# ARC Trading Platform — FastAPI Entry Point
#
# MODULES INCLUDED:
#   Module 1  — Auth          (login, logout, refresh, me)
#   Module 2  — Users/Brokers (create, manage, roles)
#   Module 3  — KYC           (submit, review, approve)
#   Module 4  — Instruments   (list, search, toggle)
#   Module 5  — Market Data   (quotes, ohlc, websocket, simulator)
#   Module 6  — Orders        (place, cancel, list, detail)
#   Module 7  — Execution     (limit watcher, fills, positions)
#   Module 8  — Portfolio     (summary, positions, holdings, trades, pnl)
#   Module 10 — Funding       (credit, debit, balance, ledger)
# ─────────────────────────────────────────────────────────────────────────

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.database import Base, engine
from app.models import *  # noqa: registers all SQLAlchemy models


# =============================================================================
# LIFESPAN — startup and shutdown events
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    # ── STARTUP ───────────────────────────────────────────────────────────

    print("=== ARC Trading Platform starting ===")

    # Step 1 — Create all DB tables (safe — skips existing tables)
    Base.metadata.create_all(bind=engine)
    print("=== Tables ready ===")

    # Step 2 — Redis connection
    from app.core.redis import init_redis_pool
    redis_ok = await init_redis_pool()
    print(f"=== Redis: {'connected' if redis_ok else 'not available (using in-memory fallback)'} ===")

    # Step 3 — OHLC Refresher
    from app.workers.ohlc_refresher import ohlc_refresher
    try:
        await ohlc_refresher.start()
        print("=== OHLC Refresher started ===")
    except Exception as exc:
        print(f"=== OHLC Refresher failed: {exc} ===")

    # Step 4 — Limit Order Watcher (Module 7 — Execution Engine)
    # Runs every 1 second — fills limit orders, fires SL/TP triggers
    from app.workers.limit_watcher import limit_watcher
    try:
        await limit_watcher.start()
        print("=== Limit Order Watcher started ===")
    except Exception as exc:
        print(f"=== Limit Order Watcher failed: {exc} ===")

    # Step 5 — WebSocket heartbeat
    from app.services.marketdata.ws_manager import manager
    try:
        await manager.start_heartbeat()
        print("=== WS heartbeat started ===")
    except Exception as exc:
        print(f"=== WS heartbeat failed: {exc} ===")

    yield

    # ── SHUTDOWN ──────────────────────────────────────────────────────────
    try:
        await ohlc_refresher.stop()
    except Exception:
        pass
    try:
        from app.workers.limit_watcher import limit_watcher
        await limit_watcher.stop()
    except Exception:
        pass
    try:
        await manager.stop_heartbeat()
    except Exception:
        pass
    try:
        from app.core.redis import close_redis_pool
        await close_redis_pool()
    except Exception:
        pass
    try:
        from app.services.marketdata import price_simulator
        price_simulator.stop()
    except Exception:
        pass
    print("=== Shutdown complete ===")


# =============================================================================
# FASTAPI APP INSTANCE
# =============================================================================

app = FastAPI(
    title       = "ARC Trading Platform — Backend API",
    description = (
        "ARC Trading Platform backend "
        "Modules: Auth, Users, KYC, Funding, Instruments, "
        "Market Data, Orders, Execution Engine, Portfolio and PnL"
    ),
    version     = "2.0.0",
    lifespan    = lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

PREFIX = "/api/v1"


# =============================================================================
# ROUTERS
# =============================================================================

# Module 1 — Authentication
from app.api.v1.auth        import router as auth_router

# Module 2 — Users & Brokers
from app.api.v1.admin       import router as admin_router
from app.api.v1.user        import router as users_router

# Module 3 — KYC
from app.api.v1.kyc         import router as kyc_router

# Module 4 — Instruments
from app.api.v1.instruments import router as instruments_router

# Module 5 — Market Data
from app.api.v1.quotes      import router as quotes_router
from app.api.v1.ohlc        import router as ohlc_router
from app.api.v1.market      import router as market_router
from app.api.v1.ws          import router as ws_router

# Module 6 — Orders
from app.api.v1.order       import router as order_router

# Module 8 — Portfolio & PnL
from app.api.v1.portfolio   import router as portfolio_router

# Module 10 — Funding
from app.api.v1.funding     import router as funding_router


# =============================================================================
# REGISTER ALL ROUTERS
# =============================================================================

app.include_router(auth_router,        prefix=PREFIX)   # Module 1
app.include_router(admin_router,       prefix=PREFIX)   # Module 2
app.include_router(users_router,       prefix=PREFIX)   # Module 2
app.include_router(kyc_router,         prefix=PREFIX)   # Module 3
app.include_router(instruments_router, prefix=PREFIX)   # Module 4
app.include_router(quotes_router,      prefix=PREFIX)   # Module 5
app.include_router(ohlc_router,        prefix=PREFIX)   # Module 5
app.include_router(market_router,      prefix=PREFIX)   # Module 5
app.include_router(ws_router,          prefix=PREFIX)   # Module 5
app.include_router(order_router,       prefix=PREFIX)   # Module 6
app.include_router(portfolio_router,   prefix=PREFIX)   # Module 8
app.include_router(funding_router,     prefix=PREFIX)   # Module 10


# =============================================================================
# SYSTEM ENDPOINTS
# =============================================================================

@app.get("/health", tags=["System"])
def health():
    """System health — shows all background worker status."""
    from app.services.marketdata import price_simulator
    from app.core.redis import is_redis_available
    from app.workers.limit_watcher import limit_watcher
    return {
        "status"                : "ok",
        "version"               : "2.0.0",
        "modules"               : [
            "auth", "users", "kyc", "instruments",
            "market_data", "orders", "execution_engine",
            "portfolio", "funding",
        ],
        "limit_watcher_running" : limit_watcher.is_running,
        "simulator_running"     : price_simulator.is_running(),
        "redis_connected"       : is_redis_available(),
    }


@app.get("/", tags=["System"])
def root():
    return {
        "project" : "ARC Trading Platform",
        "docs"    : "/docs",
        "health"  : "/health",
        "version" : "2.0.0",
    }


from fastapi.responses import FileResponse
import os

@app.get("/ws-test", include_in_schema=False)
def ws_test_page():
    path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static", "ws_test.html"
    )
    if os.path.exists(path):
        return FileResponse(path)
    return {"message": "ws_test.html not found — create static/ws_test.html"}