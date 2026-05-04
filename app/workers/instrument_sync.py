from __future__ import annotations

import asyncio, logging, signal, sys

from app.core.config                        import settings
from app.core.database                      import AsyncSessionLocal
from app.integrations.binance.adapter       import BinanceAdapter
from app.integrations.kite.adapter          import KiteAdapter
from app.models.instruments                 import PriceSource
from app.services.instruments.sync_service  import InstrumentSyncService

logger = logging.getLogger(__name__)
_STOP  = asyncio.Event()


def _handle(sig, _): _STOP.set()


async def run_kite_sync() -> None:
    logger.info("=== Kite Instrument Sync ===")
    async with AsyncSessionLocal() as db:
        svc = InstrumentSyncService(
            db,
            KiteAdapter(settings.KITE_API_KEY, settings.KITE_ACCESS_TOKEN),
            BinanceAdapter(settings.BINANCE_API_KEY),
        )
        r = await svc.sync_kite(exchanges=["NSE", "BSE", "NFO"])
    logger.info(
        "Kite sync: %s | created=%d updated=%d deactivated=%d",
        r.status, r.total_created, r.total_updated, r.total_deactivated,
    )


async def run_binance_sync() -> None:
    logger.info("=== Binance Instrument Sync ===")
    async with AsyncSessionLocal() as db:
        svc = InstrumentSyncService(
            db,
            KiteAdapter(settings.KITE_API_KEY, settings.KITE_ACCESS_TOKEN),
            BinanceAdapter(settings.BINANCE_API_KEY),
        )
        r = await svc.sync_binance()
    logger.info(
        "Binance sync: %s | created=%d updated=%d",
        r.status, r.total_created, r.total_updated,
    )


async def worker_loop() -> None:
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT,  _handle)
    KITE_SECS = 24 * 3600
    BIN_SECS  =  6 * 3600
    loop = asyncio.get_event_loop()
    last_kite = last_bin = -99999

    while not _STOP.is_set():
        now = loop.time()
        if now - last_kite >= KITE_SECS:
            try:
                await run_kite_sync()
            except Exception as e:
                logger.exception("Kite error: %s", e)
            last_kite = loop.time()
        if now - last_bin >= BIN_SECS:
            try:
                await run_binance_sync()
            except Exception as e:
                logger.exception("Binance error: %s", e)
            last_bin = loop.time()
        try:
            await asyncio.wait_for(_STOP.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass

    logger.info("Worker stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    mode = sys.argv[1] if len(sys.argv) > 1 else "loop"
    if   mode == "kite":    asyncio.run(run_kite_sync())
    elif mode == "binance": asyncio.run(run_binance_sync())
    else:                   asyncio.run(worker_loop())
