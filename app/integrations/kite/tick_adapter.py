# app/integrations/kite/tick_adapter.py
# ─────────────────────────────────────────────────────────────────────────
# Kite Connect Live Tick Ingestion Skeleton
#
# FIX vs ZIP A: uses app.models.instrument (singular) — correct base project path
# Uses quote_service.ingest_tick() → price_cache + WS fanout
# ─────────────────────────────────────────────────────────────────────────
from __future__ import annotations
import asyncio, logging
from app.services.marketdata.normalizer import normalize_kite_tick
from app.services.marketdata.quote_service import ingest_tick

logger = logging.getLogger(__name__)


class KiteTickAdapter:
    """
    Skeleton. Replace _SKELETON_ loop with KiteTicker in production:
        from kiteconnect import KiteTicker
        kws = KiteTicker(self._api_key, self._access_token)
        kws.on_ticks   = self._on_ticks_sync
        kws.on_connect = self._on_connect
        kws.connect(threaded=True)
    """

    def __init__(self, api_key: str, access_token: str, instrument_tokens: dict):
        self._api_key      = api_key
        self._access_token = access_token
        self._token_map    = instrument_tokens   # {int_token: "NSE:RELIANCE"}
        self._running      = False

    async def start(self):
        self._running = True
        logger.info("KiteTickAdapter started (%d instruments)", len(self._token_map))
        while self._running:   # _SKELETON_: replace with KiteTicker
            await asyncio.sleep(1)

    async def stop(self):
        self._running = False

    async def process_ticks(self, ticks: list[dict]):
        for raw in ticks:
            token  = raw.get("instrument_token")
            symbol = self._token_map.get(token)
            if not symbol:
                continue
            raw["_symbol"]   = symbol
            raw["_exchange"] = symbol.split(":")[0] if ":" in symbol else "NSE"
            quote = normalize_kite_tick(raw)
            if quote:
                await ingest_tick(quote)

    def _on_ticks_sync(self, ws, ticks):
        import asyncio as _a
        _a.run(self.process_ticks(ticks))

    def _on_connect(self, ws, response):
        tokens = list(self._token_map.keys())
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)


def build_token_map_from_db() -> dict:
    """Build {instrument_token: symbol} from DB. Called once at startup."""
    from app.core.database import SessionLocal
    from app.models.instruments import Instrument, PriceSource  # ← FIXED: singular
    db = SessionLocal()
    try:
        rows = db.query(Instrument).filter(
            Instrument.price_source == PriceSource.KITE,
            Instrument.is_active == True,
        ).all()
        return {
            int(i.external_token): i.symbol
            for i in rows
            if i.external_token and str(i.external_token).isdigit()
        }
    finally:
        db.close()
