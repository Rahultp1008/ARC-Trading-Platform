# app/integrations/kite/adapter.py
# Module 4 — Instrument Master | ARC Trading Platform
#
# INSTRUMENT LIMIT: 100 total across all sources.
# Kite contributes: 20 top NSE equity + 30 Nifty50 components + 30 top ETFs = 80
# (Binance contributes the remaining 20 crypto)
#
# Allowlists are enforced here so the sync never imports more than needed.
from __future__ import annotations

import csv, io, logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

import httpx
from app.models.instruments import InstrumentType, OptionType, PriceSource, Segment

logger = logging.getLogger(__name__)

KITE_URL = "https://api.kite.trade/instruments"

_SEG  = {
    "NSE": Segment.CASH, "BSE": Segment.CASH,
    "NFO-FUT": Segment.FNO, "NFO-OPT": Segment.FNO,
    "BFO-FUT": Segment.FNO, "BFO-OPT": Segment.FNO,
}
_TYPE = {
    "EQ": InstrumentType.EQUITY,
    "ETF": InstrumentType.ETF,
    "FUT": InstrumentType.FUTURES,
    "CE": InstrumentType.OPTIONS,
    "PE": InstrumentType.OPTIONS,
    "IDX": InstrumentType.INDEX,
}

# ── Allowlists — enforced to cap total instruments at 80 from Kite ─────────

# Top 20 NSE equity stocks by market cap (as of 2024)
TOP_20_EQUITY = {
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
    "SBIN", "WIPRO", "BAJFINANCE", "HINDUNILVR", "KOTAKBANK",
    "AXISBANK", "LT", "ADANIENT", "ADANIPORTS", "TATAMOTORS",
    "HCLTECH", "SUNPHARMA", "MARUTI", "ASIANPAINT", "ULTRACEMCO",
}

# Top 30 Nifty 50 components (indices and their trading symbols)
TOP_30_NIFTY50 = {
    # Nifty index symbols (IDX type)
    "NIFTY 50", "NIFTY BANK", "NIFTY IT", "NIFTY PHARMA",
    "NIFTY AUTO", "NIFTY FMCG", "NIFTY METAL", "NIFTY REALTY",
    "NIFTY MIDCAP 50", "NIFTY SMALLCAP 50",
    # Also include top Nifty50 constituents as equity for trading
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
    "HDFC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT",
    "AXISBANK", "ITC", "BAJFINANCE", "WIPRO", "HCLTECH",
    "ASIANPAINT", "MARUTI", "ULTRACEMCO", "ONGC", "TITAN",
}

# Top 30 ETFs listed on NSE
TOP_30_ETF = {
    "NIFTYBEES", "BANKBEES", "ITBEES", "JUNIORBEES", "GOLDBEES",
    "LIQUIDBEES", "CPSEETF", "PSUBNKBEES", "INFRABEES", "PHARMABEES",
    "SETFNIF50", "SETFNN50", "ICICINIFTY", "HDFCSENSEX", "SBIETFNIFTY",
    "KOTAKNIFTY", "AXISNIFTY", "MAFANG", "MIRAE_ASSET", "ICICIB22",
    "N100BEES", "MOM100", "SMALLCAP", "MIDCAP", "CONSUMPTION",
    "FINIETF", "DIVOPPBEES", "QUAL30IETF", "EVINDIA", "ALPHAETF",
}


class KiteAdapter:

    def __init__(self, api_key: str, access_token: str) -> None:
        self._headers = {
            "X-Kite-Version": "3",
            "Authorization": f"token {api_key}:{access_token}",
        }

    async def _fetch_csv(self, exchange: Optional[str] = None) -> list[dict]:
        url = f"{KITE_URL}/{exchange.upper()}" if exchange else KITE_URL
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(url, headers=self._headers)
            r.raise_for_status()
        return list(csv.DictReader(io.StringIO(r.text)))

    def normalise(self, raw: dict) -> Optional[dict[str, Any]]:
        try:
            kite_type = raw.get("instrument_type", "").strip().upper()
            kite_seg  = raw.get("segment", "").strip().upper()
            exchange  = raw.get("exchange", "").strip().upper()
            sym       = raw.get("tradingsymbol", "").strip().upper()
            if not sym or not exchange:
                return None

            arc_type = _TYPE.get(kite_type)
            if arc_type is None:
                return None

            expiry: Optional[date] = None
            if raw.get("expiry", "").strip():
                try:
                    expiry = datetime.strptime(raw["expiry"].strip(), "%Y-%m-%d").date()
                except ValueError:
                    pass

            return {
                "symbol":            f"{exchange}:{sym}",
                "external_symbol":   sym,
                "external_token":    raw.get("instrument_token", "").strip(),
                "exchange":          exchange,
                "display_name":      (raw.get("name") or sym).strip(),
                "instrument_type":   arc_type,
                "segment":           _SEG.get(kite_seg, Segment.CASH),
                "tick_size":         Decimal(raw.get("tick_size") or "0.05"),
                "lot_size":          Decimal(raw.get("lot_size") or "1"),
                "expiry":            expiry,
                "strike":            Decimal(raw["strike"]) if raw.get("strike") else None,
                "option_type":       (
                    OptionType.CE if kite_type == "CE" else
                    OptionType.PE if kite_type == "PE" else None
                ),
                "underlying_symbol": None,
                "price_source":      PriceSource.KITE,
                "is_active":         True,
                "trading_allowed":   kite_type != "IDX",
                "is_index":          kite_type == "IDX",
                "isin":              None,
                "series":            raw.get("series", "").strip() or None,
                "metadata_json": {
                    "exchange_token": raw.get("exchange_token"),
                    "kite_segment":   kite_seg,
                    "kite_type":      kite_type,
                },
            }
        except Exception as exc:
            logger.warning("Kite normalise failed: %s — %s", raw, exc)
            return None

    def _passes_allowlist(self, instrument: dict) -> bool:
        """
        Returns True if the instrument is in one of our curated allowlists.
        Enforces the 80-instrument Kite cap:
          - 20 equity (TOP_20_EQUITY)
          - 30 Nifty50 components (TOP_30_NIFTY50)
          - 30 ETFs (TOP_30_ETF)
        """
        sym = instrument.get("external_symbol", "").upper()
        itype = instrument.get("instrument_type")

        if itype == InstrumentType.EQUITY:
            return sym in TOP_20_EQUITY
        if itype == InstrumentType.INDEX:
            return sym in TOP_30_NIFTY50
        if itype == InstrumentType.ETF:
            return sym in TOP_30_ETF
        # F&O, futures, options — excluded from this platform's 100-cap config
        return False

    async def get_normalised_instruments(
        self, exchanges: Optional[list[str]] = None
    ) -> list[dict[str, Any]]:
        seen: dict[str, dict] = {}
        # Only fetch NSE — that's where our allowlisted instruments live
        for ex in (exchanges or ["NSE"]):
            try:
                rows = await self._fetch_csv(exchange=ex)
                logger.info("Kite: fetched %d rows for %s", len(rows), ex)
                for row in rows:
                    n = self.normalise(row)
                    if n and self._passes_allowlist(n):
                        seen[n["symbol"]] = n
            except Exception as exc:
                logger.error("Kite fetch failed for %s: %s", ex, exc)

        logger.info("Kite: returning %d allowlisted instruments", len(seen))
        return list(seen.values())
