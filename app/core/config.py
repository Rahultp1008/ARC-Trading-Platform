from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    DATABASE_URL        : str
    SECRET_KEY          : str
    REFRESH_SECRET_KEY  : str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS  : int = 7
    ALGORITHM           : str = "HS256"

    # ADDED (Instrument Module integrations)
    KITE_API_KEY        : str | None = None
    KITE_ACCESS_TOKEN   : str | None = None
    BINANCE_API_KEY     : str | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()