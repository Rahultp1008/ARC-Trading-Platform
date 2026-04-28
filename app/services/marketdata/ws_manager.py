# app/services/marketdata/ws_manager.py
# ─────────────────────────────────────────────────────────────────────────
# WebSocket Connection Manager — MERGED BEST OF ZIP A + ZIP B
#
# From ZIP B: backward compat exports (manager + ws_manager), subscribe_all,
#             send_personal, get_subscriptions, action-based protocol
# From ZIP A: reverse symbol→clients index for O(1) fanout,
#             WS heartbeat, max connections enforcement, connect returns bool
#
# Exports:
#   manager    → imported by ws.py  (from app.services.marketdata.ws_manager import manager)
#   ws_manager → imported by tick adapters
# Both point to the SAME singleton object.
# ─────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:

    def __init__(self) -> None:
        # ws → set of subscribed symbols
        self._connections: dict[WebSocket, set[str]] = {}
        # symbol → set of ws (reverse index for O(1) fanout)
        self._symbol_index: dict[str, set[WebSocket]] = {}
        # heartbeat task
        self._hb_task: Optional[asyncio.Task] = None

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def active_count(self) -> int:
        return len(self._connections)

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self, ws: WebSocket) -> bool:
        """Accept a WebSocket connection. Returns False if at capacity."""
        from app.core.config import settings
        max_conn = getattr(settings, "WS_MAX_CONNECTIONS", 100)
        if self.connection_count >= max_conn:
            await ws.close(code=1008, reason="Max connections reached")
            return False
        await ws.accept()
        self._connections[ws] = set()
        logger.info("WS connected. Total: %d", self.connection_count)
        return True

    def disconnect(self, ws: WebSocket) -> None:
        """Remove a client and clean up its subscriptions."""
        for sym in self._connections.pop(ws, set()):
            clients = self._symbol_index.get(sym)
            if clients:
                clients.discard(ws)
                if not clients:
                    del self._symbol_index[sym]
        logger.info("WS disconnected. Total: %d", self.connection_count)

    # ── Subscriptions ─────────────────────────────────────────────────────

    def subscribe(self, ws: WebSocket, symbols: list[str]) -> None:
        if ws not in self._connections:
            return
        for s in symbols:
            s = s.upper()
            self._connections[ws].add(s)
            self._symbol_index.setdefault(s, set()).add(ws)

    def unsubscribe(self, ws: WebSocket, symbols: list[str]) -> None:
        if ws not in self._connections:
            return
        for s in symbols:
            s = s.upper()
            self._connections[ws].discard(s)
            clients = self._symbol_index.get(s)
            if clients:
                clients.discard(ws)
                if not clients:
                    del self._symbol_index[s]

    def get_subscriptions(self, ws: WebSocket) -> set[str]:
        return self._connections.get(ws, set())

    # ── Fanout ────────────────────────────────────────────────────────────

    async def broadcast_tick(self, symbol: str, data: dict) -> int:
        """
        Send a tick to all clients subscribed to symbol OR subscribe_all (*).
        Uses reverse index for O(1) lookup instead of iterating all clients.
        """
        # Clients subscribed to this specific symbol
        clients = self._symbol_index.get(symbol.upper(), set()).copy()
        # Clients subscribed to everything (*)
        all_clients = self._symbol_index.get("*", set()).copy()
        targets = clients | all_clients

        if not targets:
            return 0

        dead: list[WebSocket] = []
        sent = 0
        for ws in targets:
            try:
                await ws.send_json(data)
                sent += 1
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)
        return sent

    async def fan_out(self, symbol: str, data: dict) -> int:
        """Alias for broadcast_tick — used by tick adapters."""
        return await self.broadcast_tick(symbol, data)

    async def broadcast(self, message: str) -> int:
        """Send a text message to ALL connected clients."""
        dead: list[WebSocket] = []
        sent = 0
        for ws in list(self._connections):
            try:
                await ws.send_text(message)
                sent += 1
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)
        return sent

    async def send_personal(self, ws: WebSocket, data: dict) -> None:
        """Send to one specific client."""
        try:
            await ws.send_json(data)
        except Exception:
            self.disconnect(ws)

    async def send_error(self, ws: WebSocket, message: str, code: str = "ERR") -> None:
        try:
            await ws.send_json({"type": "error", "message": message, "code": code})
        except Exception:
            self.disconnect(ws)

    # ── Heartbeat ─────────────────────────────────────────────────────────

    async def start_heartbeat(self) -> None:
        """Start periodic heartbeat to keep connections alive."""
        if self._hb_task is None or self._hb_task.done():
            self._hb_task = asyncio.create_task(self._heartbeat_loop())
            logger.info("WS heartbeat started.")

    async def stop_heartbeat(self) -> None:
        if self._hb_task and not self._hb_task.done():
            self._hb_task.cancel()
            try:
                await self._hb_task
            except asyncio.CancelledError:
                pass
            logger.info("WS heartbeat stopped.")

    async def _heartbeat_loop(self) -> None:
        from app.core.config import settings
        interval = getattr(settings, "WS_HEARTBEAT_INTERVAL", 30)
        while True:
            try:
                await asyncio.sleep(interval)
                hb = json.dumps({
                    "type": "heartbeat",
                    "server_time": datetime.now(timezone.utc).isoformat(),
                })
                await self.broadcast(hb)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Heartbeat error: %s", exc)


# ── Singletons — both names, same object ──────────────────────────────────
manager    = ConnectionManager()
ws_manager = manager
