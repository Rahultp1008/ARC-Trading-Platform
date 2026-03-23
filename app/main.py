from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1.auth import router as auth_router

# This variable MUST be named "app"
app = FastAPI(
    title="ARC Trading — Auth Service",
    description="Authentication & Authorization API for ARC Trading.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api/v1")

@app.get("/")
def root():
    return {
        "project" : "ARC Trading Platform",
        "status"  : "running",
        "docs"    : "http://127.0.0.1:8000/docs",
        
    }