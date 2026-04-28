# app/core/config.py  
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Modules 1–4 (unchanged) ───────────────────────────────────────────
    DATABASE_URL: str = "postgresql://postgres:admin123@localhost:5432/arc_trading"
    DB_ECHO: bool = False
    SECRET_KEY: str = "change-me"
    REFRESH_SECRET_KEY: str = "change-me-too"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    KITE_API_KEY: str = ""
    KITE_ACCESS_TOKEN: str = ""
    BINANCE_API_KEY: str = ""

    # ── Module 5 — Redis (optional) ───────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_MAX_CONNECTIONS: int = 20

    # ── Module 5 — Staleness thresholds (seconds) ─────────────────────────
    QUOTE_STALE_THRESHOLD_EQUITY: int = 5    # NSE/BSE
    QUOTE_STALE_THRESHOLD_CRYPTO: int = 10   # Binance
    QUOTE_STALE_THRESHOLD_DEFAULT: int = 15  # fallback

    # ── Module 5 — OHLC ───────────────────────────────────────────────────
    OHLC_INTERVALS: list[str] = ["1m", "5m", "15m", "1h", "1d"]
    OHLC_CANDLE_TTL_SECONDS: int = 86400

    # ── Module 5 — WebSocket ──────────────────────────────────────────────
    WS_MAX_CONNECTIONS: int = 100
    WS_HEARTBEAT_INTERVAL: int = 30
    BINANCE_WS_BASE_URL: str = "wss://stream.binance.com:9443/ws"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
