# app/services/instruments/sync_service.py
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.integrations.binance.adapter import BinanceAdapter
from app.integrations.kite.adapter import KiteAdapter
from app.models.instruments import Instrument, InstrumentSyncLog, PriceSource
from app.schemas.instrument import InstrumentSyncResult
from app.services.instruments.instrument_service import InstrumentService

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run an async coroutine safely from a sync context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


def _build_result(
    source: str,            # must be 'kite', 'binance', or 'manual'
    started_at: datetime,
    finished_at: datetime,
    total_fetched: int = 0,
    total_created: int = 0,
    total_updated: int = 0,
    total_deactivated: int = 0,
    errors: list | None = None,
    status: str = "success",
) -> InstrumentSyncResult:
    """Build InstrumentSyncResult — auto-maps fields to whatever the schema has."""
    errors = errors or []
    fields = InstrumentSyncResult.model_fields.keys()
    duration = round((finished_at - started_at).total_seconds(), 2)

    data: dict = {}

    # source — must be enum value: kite, binance, or manual
    if "source"            in fields: data["source"]            = source
    if "status"            in fields: data["status"]            = status
    if "total_fetched"     in fields: data["total_fetched"]     = total_fetched
    if "total_created"     in fields: data["total_created"]     = total_created
    if "total_updated"     in fields: data["total_updated"]     = total_updated
    if "total_deactivated" in fields: data["total_deactivated"] = total_deactivated
    if "errors"            in fields: data["errors"]            = errors
    if "started_at"        in fields: data["started_at"]        = started_at
    if "finished_at"       in fields: data["finished_at"]       = finished_at
    if "duration_seconds"  in fields: data["duration_seconds"]  = duration
    # legacy field names
    if "upserted"          in fields: data["upserted"]          = total_created + total_updated
    if "deactivated"       in fields: data["deactivated"]       = total_deactivated
    if "synced_at"         in fields: data["synced_at"]         = finished_at

    return InstrumentSyncResult(**data)


def _upsert_batch(db: Session, raw: list[dict]) -> tuple[int, int]:
    """Upsert a list of normalised instrument dicts. Returns (created, updated)."""
    created = updated = 0
    for data in raw:
        existing = (
            db.query(Instrument)
            .filter(Instrument.symbol == data["symbol"])
            .first()
        )
        if existing:
            for k, v in data.items():
                if hasattr(existing, k):
                    setattr(existing, k, v)
            updated += 1
        else:
            db.add(Instrument(
                id=uuid.uuid4(),
                **{k: v for k, v in data.items() if hasattr(Instrument, k)}
            ))
            created += 1
    db.commit()
    return created, updated


class InstrumentSyncService:

    def __init__(
        self,
        db: Session,
        kite_adapter: KiteAdapter,
        binance_adapter: BinanceAdapter,
    ) -> None:
        self.db      = db
        self.kite    = kite_adapter
        self.binance = binance_adapter
        self._svc    = InstrumentService(db)

    # ── Kite sync ─────────────────────────────────────────────────────────

    def sync_kite(self, exchanges: list[str] | None = None) -> InstrumentSyncResult:
        started = datetime.now(timezone.utc)
        errors: list[str] = []
        total_fetched = total_created = total_updated = total_deactivated = 0

        try:
            raw = _run_async(
                self.kite.get_normalised_instruments(exchanges=exchanges)
            )
            total_fetched = len(raw)
            total_created, total_updated = _upsert_batch(self.db, raw)
            total_deactivated = self._svc.deactivate_expired()
        except Exception as exc:
            logger.error("Kite sync failed: %s", exc, exc_info=True)
            errors.append(str(exc))

        finished = datetime.now(timezone.utc)
        self._write_log(
            PriceSource.KITE,
            total_created + total_updated,
            total_deactivated,
            errors[0] if errors else None,
        )
        return _build_result(
            source="kite",
            started_at=started,
            finished_at=finished,
            total_fetched=total_fetched,
            total_created=total_created,
            total_updated=total_updated,
            total_deactivated=total_deactivated,
            errors=errors,
            status="error" if errors else "success",
        )

    # ── Binance sync ───────────────────────────────────────────────────────

    def sync_binance(self) -> InstrumentSyncResult:
        started = datetime.now(timezone.utc)
        errors: list[str] = []
        total_fetched = total_created = total_updated = 0

        try:
            raw = _run_async(self.binance.get_normalised_instruments())
            total_fetched = len(raw)
            total_created, total_updated = _upsert_batch(self.db, raw)
        except Exception as exc:
            logger.error("Binance sync failed: %s", exc, exc_info=True)
            errors.append(str(exc))

        finished = datetime.now(timezone.utc)
        self._write_log(
            PriceSource.BINANCE,
            total_created + total_updated,
            0,
            errors[0] if errors else None,
        )
        return _build_result(
            source="binance",
            started_at=started,
            finished_at=finished,
            total_fetched=total_fetched,
            total_created=total_created,
            total_updated=total_updated,
            total_deactivated=0,
            errors=errors,
            status="error" if errors else "success",
        )

    # ── Sync all — runs kite then binance, returns combined summary ────────

    def sync_all(self) -> InstrumentSyncResult:
        started = datetime.now(timezone.utc)
        total_fetched = total_created = total_updated = total_deactivated = 0
        all_errors: list[str] = []

        # Kite
        try:
            r = self.sync_kite()
            total_fetched     += getattr(r, "total_fetched",     0)
            total_created     += getattr(r, "total_created",     0)
            total_updated     += getattr(r, "total_updated",     0)
            total_deactivated += getattr(r, "total_deactivated", 0)
            all_errors.extend(getattr(r, "errors", []))
        except Exception as exc:
            all_errors.append(f"kite: {exc}")

        # Binance
        try:
            r = self.sync_binance()
            total_fetched += getattr(r, "total_fetched", 0)
            total_created += getattr(r, "total_created", 0)
            total_updated += getattr(r, "total_updated", 0)
            all_errors.extend(getattr(r, "errors", []))
        except Exception as exc:
            all_errors.append(f"binance: {exc}")

        finished = datetime.now(timezone.utc)

        # source must be a valid enum — use 'manual' for combined sync
        return _build_result(
            source="manual",
            started_at=started,
            finished_at=finished,
            total_fetched=total_fetched,
            total_created=total_created,
            total_updated=total_updated,
            total_deactivated=total_deactivated,
            errors=all_errors,
            status="partial" if all_errors else "success",
        )

    def _write_log(
        self,
        source: PriceSource,
        upserted: int,
        deactivated: int,
        error: str | None,
    ) -> None:
        try:
            log = InstrumentSyncLog(
                source=source,
                instruments_upserted=upserted,
                instruments_deactivated=deactivated,
                error=error,
                synced_at=datetime.now(timezone.utc),
            )
            self.db.add(log)
            self.db.commit()
        except Exception as exc:
            logger.warning("Could not write sync log: %s", exc)
