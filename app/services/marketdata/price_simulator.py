"""
price_simulator.py — Generates realistic fake price ticks for testing.

Simulates price movement for all seeded instruments so you can test:
  - WebSocket live feeds
  - Quote APIs
  - OHLC candle building
  - Portfolio unrealized PnL (later)

Run standalone: python -m app.services.marketdata.price_simulator
Or started via the /api/v1/market/simulator/start endpoint.
"""
import asyncio
import logging
import random
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

from app.services.marketdata import price_cache

logger = logging.getLogger(__name__)

# Base prices for each instrument (approximate real prices in INR / USD)
BASE_PRICES = {
    # NSE Equities (INR)
    "NSE:RELIANCE": 1350.0, "NSE:TCS": 3900.0, "NSE:HDFCBANK": 1850.0,
    "NSE:INFY": 1550.0, "NSE:ICICIBANK": 1350.0, "NSE:HINDUNILVR": 2450.0,
    "NSE:SBIN": 800.0, "NSE:BHARTIARTL": 1750.0, "NSE:ITC": 440.0,
    "NSE:KOTAKBANK": 1900.0, "NSE:LT": 3500.0, "NSE:AXISBANK": 1150.0,
    "NSE:ASIANPAINT": 2800.0, "NSE:MARUTI": 12500.0, "NSE:HCLTECH": 1650.0,
    "NSE:SUNPHARMA": 1800.0, "NSE:TITAN": 3200.0, "NSE:BAJFINANCE": 6800.0,
    "NSE:WIPRO": 450.0, "NSE:ULTRACEMCO": 11000.0, "NSE:ONGC": 260.0,
    "NSE:NTPC": 350.0, "NSE:POWERGRID": 310.0, "NSE:TATAMOTORS": 680.0,
    "NSE:TATASTEEL": 155.0, "NSE:M&M": 2800.0, "NSE:TECHM": 1600.0,
    "NSE:JSWSTEEL": 920.0, "NSE:ADANIENT": 2400.0, "NSE:ADANIPORTS": 1350.0,
    "NSE:COALINDIA": 420.0, "NSE:BPCL": 310.0, "NSE:DRREDDY": 6200.0,
    "NSE:EICHERMOT": 4800.0, "NSE:GRASIM": 2600.0, "NSE:INDUSINDBK": 1000.0,
    "NSE:CIPLA": 1500.0, "NSE:DIVISLAB": 4100.0, "NSE:APOLLOHOSP": 6500.0,
    "NSE:HEROMOTOCO": 4300.0,
    # Indices
    "NSE:NIFTY 50": 24500.0, "NSE:NIFTYBANK": 52000.0, "NSE:NIFTYIT": 35000.0,
    "NSE:NIFTYFMCG": 55000.0, "NSE:NIFTYPHARMA": 20000.0, "NSE:NIFTYAUTO": 24000.0,
    "NSE:NIFTYMETAL": 8500.0, "NSE:NIFTYENERGY": 38000.0, "NSE:NIFTYMIDCAP": 14500.0,
    "NSE:NIFTYSMALLCAP": 8000.0,
    # ETFs
    "NSE:NIFTYBEES": 245.0, "NSE:BANKBEES": 520.0, "NSE:GOLDBEES": 55.0,
    "NSE:ITBEES": 350.0, "NSE:SILVERBEES": 75.0, "NSE:LIQUIDBEES": 1000.0,
    "NSE:JUNIORBEES": 680.0, "NSE:CPSEETF": 82.0, "NSE:SETFNIF50": 248.0,
    "NSE:SETFNIFBK": 525.0,
    # Crypto (USDT)
    "CRYPTO:BTCUSDT": 84500.0, "CRYPTO:ETHUSDT": 3200.0, "CRYPTO:BNBUSDT": 610.0,
    "CRYPTO:XRPUSDT": 0.52, "CRYPTO:SOLUSDT": 135.0, "CRYPTO:ADAUSDT": 0.38,
    "CRYPTO:DOGEUSDT": 0.082, "CRYPTO:TRXUSDT": 0.11, "CRYPTO:DOTUSDT": 7.2,
    "CRYPTO:MATICUSDT": 0.55, "CRYPTO:AVAXUSDT": 35.0, "CRYPTO:LTCUSDT": 85.0,
    "CRYPTO:LINKUSDT": 14.5, "CRYPTO:ATOMUSDT": 9.0, "CRYPTO:UNIUSDT": 6.5,
    "CRYPTO:ETCUSDT": 26.0, "CRYPTO:XLMUSDT": 0.12, "CRYPTO:NEARUSDT": 5.5,
    "CRYPTO:APTUSDT": 8.5, "CRYPTO:FILUSDT": 5.8, "CRYPTO:AAVEUSDT": 85.0,
    "CRYPTO:ALGOUSDT": 0.22, "CRYPTO:MANAUSDT": 0.45, "CRYPTO:SANDUSDT": 0.55,
    "CRYPTO:AXSUSDT": 7.5, "CRYPTO:SHIBUSDT": 0.000012, "CRYPTO:ICPUSDT": 12.5,
    "CRYPTO:THETAUSDT": 2.1, "CRYPTO:FTMUSDT": 0.72, "CRYPTO:RUNEUSDT": 5.3,
    "CRYPTO:GRTUSDT": 0.25, "CRYPTO:INJUSDT": 22.0, "CRYPTO:OPUSDT": 1.8,
    "CRYPTO:ARBUSDT": 1.1, "CRYPTO:SUIUSDT": 3.5, "CRYPTO:SEIUSDT": 0.45,
    "CRYPTO:TIAUSDT": 9.5, "CRYPTO:JUPUSDT": 0.85, "CRYPTO:WIFUSDT": 1.2,
    "CRYPTO:PEPEUSDT": 0.0000085,
}

# Current simulated prices (mutated during simulation)
_current_prices: dict[str, float] = {}
_running = False
_thread: threading.Thread | None = None
_subscribers: list = []  # WebSocket subscribers


def _init_prices():
    global _current_prices
    _current_prices = dict(BASE_PRICES)
    # Add F&O prices derived from underlying
    for sym, price in list(BASE_PRICES.items()):
        if sym.startswith("NSE:"):
            underlying = sym.split(":")[1]
            # Futures trade at slight premium
            fut_key = f"NFO:{underlying}26MAYFUT"
            _current_prices[fut_key] = price * 1.002
    logger.info("Initialized %d simulated prices", len(_current_prices))


def _tick_once():
    """Generate one round of price ticks for all instruments."""
    now = datetime.now(timezone.utc).isoformat()
    for symbol, price in _current_prices.items():
        # Random walk: ±0.3% per tick
        change_pct = random.uniform(-0.003, 0.003)
        new_price = round(price * (1 + change_pct), 8)
        if new_price <= 0:
            new_price = price
        _current_prices[symbol] = new_price

        # Volume simulation
        volume = random.randint(100, 50000)

        # Update LTP cache
        price_cache.set_ltp(symbol, new_price, volume=volume, timestamp=now)

        # Update full quote
        spread = new_price * 0.001  # 0.1% spread
        price_cache.set_quote(symbol, {
            "symbol": symbol,
            "ltp": new_price,
            "bid": round(new_price - spread, 8),
            "ask": round(new_price + spread, 8),
            "high": round(new_price * 1.015, 8),
            "low": round(new_price * 0.985, 8),
            "open": round(BASE_PRICES.get(symbol, new_price), 8),
            "close": round(BASE_PRICES.get(symbol, new_price), 8),
            "volume": volume,
            "change": round(new_price - BASE_PRICES.get(symbol, new_price), 4),
            "change_pct": round((new_price / BASE_PRICES.get(symbol, new_price) - 1) * 100, 4),
            "timestamp": now,
            "source": "BINANCE" if "CRYPTO:" in symbol else "KITE",
        })

    # Update market statuses
    price_cache.set_market_status("NSE", "open")
    price_cache.set_market_status("NFO", "open")
    price_cache.set_market_status("BINANCE", "open")


def _build_ohlc():
    """Build 1-minute OHLC candles from current prices."""
    now = datetime.now(timezone.utc)
    for symbol, price in _current_prices.items():
        base = BASE_PRICES.get(symbol, price)
        candle = {
            "symbol": symbol,
            "interval": "1m",
            "open": round(base, 8),
            "high": round(max(price, base) * 1.002, 8),
            "low": round(min(price, base) * 0.998, 8),
            "close": round(price, 8),
            "volume": random.randint(1000, 100000),
            "timestamp": now.isoformat(),
        }
        price_cache.set_ohlc(symbol, "1m", candle)

        # Build a series of 30 candles for charting
        series = price_cache.get_ohlc_series(symbol, "1m")
        if len(series) >= 30:
            series = series[-29:]
        series.append(candle)
        price_cache.set_ohlc_series(symbol, "1m", series)


def _run_loop():
    global _running
    _init_prices()
    logger.info("Price simulator started — ticking every 2 seconds")
    while _running:
        _tick_once()
        _build_ohlc()
        time.sleep(2)
    logger.info("Price simulator stopped")


def start():
    global _running, _thread
    if _running:
        return False
    _running = True
    _thread = threading.Thread(target=_run_loop, daemon=True)
    _thread.start()
    return True


def stop():
    global _running
    _running = False
    return True


def is_running() -> bool:
    return _running


def get_current_price(symbol: str) -> float | None:
    return _current_prices.get(symbol)


def get_all_current_prices() -> dict[str, float]:
    return dict(_current_prices)
