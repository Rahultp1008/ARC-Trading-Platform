# app/integrations/binance/adapter.py
# Module 4 — Instrument Master | ARC Trading Platform
# Paper / simulated trading only per spec MVP scope.
#
# INSTRUMENT LIMIT: Top 20 USDT pairs only.
# These 20 crypto pairs fill the remaining slots in the 100-instrument cap
# (Kite provides 80, Binance provides 20).
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

import httpx
from app.models.instruments import InstrumentType, PriceSource, Segment

logger = logging.getLogger(__name__)

BINANCE_URL = "https://api.binance.com/api/v3/exchangeInfo"

# ── Top 20 crypto pairs by volume/market cap (USDT only) ──────────────────
TOP_20_CRYPTO_USDT = {
    "BTCUSDT",   # Bitcoin
    "ETHUSDT",   # Ethereum
    "BNBUSDT",   # BNB
    "SOLUSDT",   # Solana
    "XRPUSDT",   # XRP
    "ADAUSDT",   # Cardano
    "DOGEUSDT",  # Dogecoin
    "MATICUSDT", # Polygon
    "DOTUSDT",   # Polkadot
    "AVAXUSDT",  # Avalanche
    "LINKUSDT",  # Chainlink
    "LTCUSDT",   # Litecoin
    "UNIUSDT",   # Uniswap
    "ATOMUSDT",  # Cosmos
    "XLMUSDT",   # Stellar
    "ETCUSDT",   # Ethereum Classic
    "NEARUSDT",  # NEAR Protocol
    "ALGOUSDT",  # Algorand
    "AAVEUSDT",  # Aave
    "FTMUSDT",   # Fantom
}


class BinanceAdapter:

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._headers = {"X-MBX-APIKEY": api_key} if api_key else {}

    async def _fetch(self) -> dict:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(BINANCE_URL, headers=self._headers)
            r.raise_for_status()
            return r.json()

    @staticmethod
    def _tick(filters: list[dict]) -> Decimal:
        for f in filters:
            if f.get("filterType") == "PRICE_FILTER":
                return Decimal(f.get("tickSize", "0.00000001"))
        return Decimal("0.00000001")

    @staticmethod
    def _qty(filters: list[dict]) -> Decimal:
        for f in filters:
            if f.get("filterType") == "LOT_SIZE":
                return Decimal(f.get("minQty", "0.00000001"))
        return Decimal("0.00000001")

    def normalise(self, raw: dict) -> Optional[dict[str, Any]]:
        try:
            sym    = raw.get("symbol",     "").strip().upper()
            base   = raw.get("baseAsset",  "").strip().upper()
            quote  = raw.get("quoteAsset", "").strip().upper()
            status = raw.get("status",     "").strip().upper()

            # ── Hard filters ───────────────────────────────────────────────
            if status != "TRADING":
                return None
            if sym not in TOP_20_CRYPTO_USDT:   # Only our top 20
                return None

            f = raw.get("filters", [])
            return {
                "symbol":            f"CRYPTO:{sym}",
                "external_symbol":   sym,
                "external_token":    sym,
                "exchange":          "BINANCE",
                "display_name":      f"{base}/{quote}",
                "instrument_type":   InstrumentType.CRYPTO,
                "segment":           Segment.CRYPTO,
                "tick_size":         self._tick(f),
                "lot_size":          self._qty(f),
                "expiry":            None,
                "strike":            None,
                "option_type":       None,
                "underlying_symbol": None,
                "price_source":      PriceSource.BINANCE,
                "is_active":         True,
                "trading_allowed":   True,
                "is_index":          False,
                "isin":              None,
                "series":            None,
                "metadata_json":     {"base_asset": base, "quote_asset": quote},
            }
        except Exception as exc:
            logger.warning("Binance normalise failed: %s — %s", raw, exc)
            return None

    async def get_normalised_instruments(self) -> list[dict[str, Any]]:
        try:
            info   = await self._fetch()
            result = [self.normalise(s) for s in info.get("symbols", [])]
            result = [r for r in result if r]
            logger.info("Binance: returning %d instruments (top 20 USDT cap)", len(result))
            return result
        except Exception as exc:
            logger.error("Binance fetch failed: %s", exc)
            return []
