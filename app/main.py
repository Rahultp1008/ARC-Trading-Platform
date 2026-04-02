# app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.auth    import router as auth_router
from app.api.v1.admin   import router as admin_router
from app.api.v1.user  import router as users_router
from app.api.v1.kyc     import router as kyc_router
from app.api.v1.funding import router as funding_router

from app.core.database import Base, engine
from app.models import *




app = FastAPI(
    title      = "ARC Trading Platform — Backend API",
    description= "Auth, User Management, KYC, and Funding modules.",
    version    = "1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

PREFIX = "/api/v1"
app.include_router(auth_router,    prefix=PREFIX)
app.include_router(admin_router,   prefix=PREFIX)
app.include_router(users_router,   prefix=PREFIX)
app.include_router(kyc_router,     prefix=PREFIX)
app.include_router(funding_router, prefix=PREFIX)

@app.get("/health", tags=["Health"])
def health():
    return {"status": "ok", "service": "arc-trading-api"}

@app.get("/", tags=["Root"])
def root():
    return {"project": "ARC Trading Platform", "docs": "/docs"}



@app.on_event("startup")
def create_tables():
    print("🔥 Creating tables...")
    Base.metadata.create_all(bind=engine)