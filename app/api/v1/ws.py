# app/api/v1/ws.py
# ─────────────────────────────────────────────────────────────────────────
# WebSocket endpoint for real-time price streaming.
# Per spec 5.5: "WebSocket fanout" and "Live push to frontend/mobile"
#
# Protocol (action-based, same as base project):
#   Client → Server:
#     {"action": "subscribe",     "symbols": ["NSE:RELIANCE", "CRYPTO:BTCUSDT"]}
#     {"action": "unsubscribe",   "symbols": ["NSE:RELIANCE"]}
#     {"action": "subscribe_all"}    → receive ALL ticks
#     {"action": "ping"}
#
#   Server → Client:
#     {"type": "connected",  "message": "...", "actions": [...]}
#     {"type": "subscribed", "symbols": [...]}
#     {"type": "tick",       "symbol": "NSE:RELIANCE", "ltp": 2450.50, ...}
#     {"type": "heartbeat",  "server_time": "..."}
#     {"type": "pong",       "server_time": "..."}
#     {"type": "error",      "message": "..."}
# ─────────────────────────────────────────────────────────────────────────
import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.services.marketdata.ws_manager import manager
from app.services.marketdata import price_cache

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/quotes")
async def websocket_quotes(ws: WebSocket):
    connected = await manager.connect(ws)
    if not connected:
        return   # max connections reached — already closed

    try:
        # Welcome message
        await manager.send_personal(ws, {
            "type": "connected",
            "message": "Connected to ARC Trading live quotes",
            "actions": ["subscribe", "unsubscribe", "subscribe_all", "ping"],
        })

        # Push ticks every 2 seconds as a background task
        push_task = asyncio.create_task(_push_ticks(ws))

        # Listen for subscribe / unsubscribe messages
        while True:
            data = await ws.receive_text()
            try:
                msg    = json.loads(data)
                action = msg.get("action", "").lower()

                if action == "subscribe":
                    symbols = msg.get("symbols", [])
                    if not isinstance(symbols, list):
                        await manager.send_error(ws, "'symbols' must be a list")
                        continue
                    manager.subscribe(ws, symbols)
                    await manager.send_personal(ws, {
                        "type":    "subscribed",
                        "symbols": list(manager.get_subscriptions(ws)),
                    })

                elif action == "unsubscribe":
                    symbols = msg.get("symbols", [])
                    manager.unsubscribe(ws, symbols)
                    await manager.send_personal(ws, {
                        "type":    "unsubscribed",
                        "symbols": list(manager.get_subscriptions(ws)),
                    })

                elif action == "subscribe_all":
                    manager.subscribe(ws, ["*"])
                    await manager.send_personal(ws, {
                        "type":    "subscribed",
                        "symbols": ["*"],
                        "note":    "Receiving all ticks",
                    })

                elif action == "ping":
                    await manager.send_personal(ws, {
                        "type":        "pong",
                        "server_time": datetime.now(timezone.utc).isoformat(),
                    })

                else:
                    await manager.send_error(
                        ws,
                        f"Unknown action '{action}'. Use: subscribe, unsubscribe, subscribe_all, ping",
                    )

            except json.JSONDecodeError:
                await manager.send_error(ws, "Invalid JSON")

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.error("WS error: %s", exc)
    finally:
        push_task.cancel()
        manager.disconnect(ws)


async def _push_ticks(ws: WebSocket) -> None:
    """Background task — pushes subscribed ticks every 2 seconds."""
    try:
        while True:
            await asyncio.sleep(2)
            subs = manager.get_subscriptions(ws)
            if not subs:
                continue

            all_ltps = price_cache.get_all_ltps()
            for ltp in all_ltps:
                symbol = ltp.get("symbol", "")
                if "*" in subs or symbol in subs:
                    tick = dict(ltp)
                    tick["type"] = "tick"
                    try:
                        await ws.send_json(tick)
                    except Exception:
                        return   # connection closed
    except asyncio.CancelledError:
        pass
