# app/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-create all tables on first run (safe to re-run — no-op if tables exist)
    from app.core.database import Base, engine
    import app.models  # noqa: registers all models with Base
    Base.metadata.create_all(bind=engine)
    print("=== ARC Trading Platform — tables ready ===")
    yield
    print("=== ARC Trading Platform — shutdown complete ===")


app = FastAPI(
    title="ARC Trading Platform — Backend API",
    description="Auth, User Management, KYC, Funding & Instruments (Modules 1–4).",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

PREFIX = "/api/v1"

from app.api.v1.auth        import router as auth_router
from app.api.v1.admin       import router as admin_router
from app.api.v1.user        import router as users_router
from app.api.v1.kyc         import router as kyc_router
from app.api.v1.funding     import router as funding_router
from app.api.v1.instruments import router as instruments_router

app.include_router(auth_router,        prefix=PREFIX)
app.include_router(admin_router,       prefix=PREFIX)
app.include_router(users_router,       prefix=PREFIX)
app.include_router(kyc_router,         prefix=PREFIX)
app.include_router(funding_router,     prefix=PREFIX)
app.include_router(instruments_router, prefix=PREFIX)


@app.get("/health", tags=["Health"])
def health():
    return {
        "status": "ok",
        "service": "arc-trading-api",
        "modules": ["auth", "users", "kyc", "funding", "instruments"],
    }


@app.get("/", tags=["Root"])
def root():
    return {"project": "ARC Trading Platform", "docs": "/docs", "version": "1.0.0"}
