# app/integrations/binance/tick_adapter.py
# Binance Live Tick Ingestion Skeleton
from __future__ import annotations
import asyncio, logging
from app.services.marketdata.normalizer import normalize_binance_tick
from app.services.marketdata.quote_service import ingest_tick

logger = logging.getLogger(__name__)

TOP_20_STREAMS = [
    "btcusdt","ethusdt","bnbusdt","solusdt","xrpusdt",
    "adausdt","dogeusdt","maticusdt","dotusdt","avaxusdt",
    "linkusdt","ltcusdt","uniusdt","atomusdt","xlmusdt",
    "etcusdt","nearusdt","algousdt","aaveusdt","ftmusdt",
]


class BinanceTickAdapter:
    """
    Skeleton. Replace _SKELETON_ loop with websockets in production:
        import websockets, json
        async with websockets.connect(self._stream_url()) as ws:
            async for msg in ws:
                tick = json.loads(msg).get("data", json.loads(msg))
                await self._process(tick)
    """

    def __init__(self, symbols=None):
        self._symbols = symbols or TOP_20_STREAMS
        self._running = False

    def _stream_url(self):
        streams = "/".join(f"{s}@ticker" for s in self._symbols)
        return f"wss://stream.binance.com:9443/ws/{streams}"

    async def start(self):
        self._running = True
        logger.info("BinanceTickAdapter started (%d pairs)", len(self._symbols))
        while self._running:   # _SKELETON_
            await asyncio.sleep(1)

    async def stop(self):
        self._running = False

    async def _process(self, raw: dict):
        quote = normalize_binance_tick(raw)
        if quote:
            await ingest_tick(quote)
