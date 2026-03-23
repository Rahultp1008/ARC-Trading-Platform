# app/core/config.py
# ---------------------------------------------------------------------------
# Centralised settings loaded from your .env file.
# Pydantic-settings automatically reads from env vars / .env so there is no
# need to scatter os.getenv() calls throughout the codebase.
# ---------------------------------------------------------------------------

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str

    # JWT — keep these long, random, and secret!
    SECRET_KEY: str
    REFRESH_SECRET_KEY: str

    # Token expiry
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15      # short-lived for security
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Algorithm used to sign JWTs
    ALGORITHM: str = "HS256"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


# Single shared instance imported everywhere
settings = Settings()
