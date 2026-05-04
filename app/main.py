# app/main.py
# ─────────────────────────────────────────────────────────────────────────
# ARC Trading Platform — FastAPI Entry Point (Modules 1–6)
# ─────────────────────────────────────────────────────────────────────────
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.database import Base, engine
from app.models import *  # noqa: registers all models


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Create DB tables
    print("=== ARC Trading Platform starting ===")
    Base.metadata.create_all(bind=engine)
    print("=== Tables ready ===")

    # 2. Try to connect Redis (graceful — server starts even if Redis is down)
    from app.core.redis import init_redis_pool
    redis_ok = await init_redis_pool()
    print(f"=== Redis: {'connected' if redis_ok else 'not available (using in-memory fallback)'} ===")

    # 3. Start OHLC Refresher
    from app.workers.ohlc_refresher import ohlc_refresher
    try:
        await ohlc_refresher.start()
        print("=== OHLC Refresher started ===")
    except Exception as exc:
        print(f"=== OHLC Refresher failed to start: {exc} ===")

    # 4. Start WS heartbeat
    from app.services.marketdata.ws_manager import manager
    try:
        await manager.start_heartbeat()
        print("=== WS heartbeat started ===")
    except Exception as exc:
        print(f"=== WS heartbeat failed: {exc} ===")

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────
    try:
        await ohlc_refresher.stop()
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


app = FastAPI(
    title="ARC Trading Platform — Backend API",
    description="Auth, Users, KYC, Funding, Instruments, Market Data, Order",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

PREFIX = "/api/v1"

# ── Modules 1–4 ───────────────────────────────────────────────────────────
from app.api.v1.auth        import router as auth_router
from app.api.v1.admin       import router as admin_router
from app.api.v1.user        import router as users_router   # FIX 1: was user.py → correct file is users.py
from app.api.v1.kyc         import router as kyc_router
from app.api.v1.funding     import router as funding_router
from app.api.v1.instruments import router as instruments_router

# ── Module 5 — Market Data ────────────────────────────────────────────────
from app.api.v1.quotes  import router as quotes_router
from app.api.v1.ohlc    import router as ohlc_router
from app.api.v1.market  import router as market_router
from app.api.v1.ws      import router as ws_router          # FIX 2: was missing entirely

# ── Module 6 — Order ────────────────────────────────────────────────────
from app.api.v1.order  import router as order_router

app.include_router(auth_router,        prefix=PREFIX)
app.include_router(admin_router,       prefix=PREFIX)
app.include_router(users_router,       prefix=PREFIX)
app.include_router(kyc_router,         prefix=PREFIX)
app.include_router(funding_router,     prefix=PREFIX)
app.include_router(instruments_router, prefix=PREFIX)
app.include_router(quotes_router,      prefix=PREFIX)
app.include_router(ohlc_router,        prefix=PREFIX)
app.include_router(market_router,      prefix=PREFIX)
app.include_router(ws_router,          prefix=PREFIX)       # FIX 2: was missing entirely
app.include_router(order_router,       prefix=PREFIX)


@app.get("/health")
def health():
    from app.services.marketdata import price_simulator
    from app.core.redis import is_redis_available
    return {
        "status": "ok",
        "version": "2.0.0",
        "modules": ["auth", "users", "kyc", "funding", "instruments", "market_data", "order"],
        "simulator_running": price_simulator.is_running(),
        "redis_connected": is_redis_available(),
    }


@app.get("/")
def root():
    return {"project": "ARC Trading Platform", "docs": "/docs", "version": "2.0.0"}


# WebSocket test page
from fastapi.responses import FileResponse
import os

@app.get("/ws-test", include_in_schema=False)
def ws_test_page():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "ws_test.html")
    if os.path.exists(path):
        return FileResponse(path)
    return {"message": "ws_test.html not found — create static/ws_test.html to use this page"}
