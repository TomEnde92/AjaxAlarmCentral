"""Databaselaag: SQLite via SQLAlchemy async.

SQLite is hier ruim voldoende — het gaat om een handvol events per dag — en
scheelt een losse databaseservice op de Pi. WAL staat aan zodat het dashboard
kan lezen terwijl de receiver schrijft.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import AlarmEvent, Base, CallAttempt, Event, NotificationLog, StateEntry, utcnow

_LOGGER = logging.getLogger(__name__)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}", future=True)

        @sa_event.listens_for(self._engine.sync_engine, "connect")
        def _set_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cursor = dbapi_conn.cursor()
            # WAL: lezers (dashboard) blokkeren de schrijver (receiver) niet.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        _LOGGER.info("Database geopend: %s", self.path)

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        if self._sessionmaker is None:
            raise RuntimeError("Database niet verbonden; roep eerst connect() aan")
        async with self._sessionmaker() as session:
            yield session

    # ── Events ───────────────────────────────────────────────────────────────

    async def store_event(self, alarm: AlarmEvent) -> int:
        """Schrijf een event weg en zet de database-id terug op het domeinobject."""
        row = Event(
            uid=alarm.uid,
            received_at=alarm.received_at,
            event_at=alarm.event_at,
            code=alarm.code,
            category=alarm.category,
            severity=alarm.severity,
            title=alarm.title,
            description=alarm.description,
            source=alarm.source,
            account=alarm.account,
            device_id=alarm.device_id,
            device_name=alarm.device_name,
            partition_id=alarm.partition_id,
            partition_name=alarm.partition_name,
            user_id=alarm.user_id,
            user_name=alarm.user_name,
            zone=alarm.zone,
            message=alarm.message,
            raw=alarm.raw,
        )
        async with self.session() as session:
            session.add(row)
            await session.commit()
            alarm.db_id = row.id
            return row.id

    async def get_event(self, event_id: int) -> Event | None:
        async with self.session() as session:
            return await session.get(Event, event_id)

    async def list_events(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        severity: str | None = None,
        exclude_severities: Sequence[str] | None = None,
        category: str | None = None,
        device_id: str | None = None,
        partition_id: str | None = None,
        since: datetime | None = None,
    ) -> Sequence[Event]:
        stmt = select(Event).order_by(Event.received_at.desc(), Event.id.desc())
        if severity:
            stmt = stmt.where(Event.severity == severity)
        elif exclude_severities:
            stmt = stmt.where(Event.severity.notin_(list(exclude_severities)))
        if category:
            stmt = stmt.where(Event.category == category)
        if device_id:
            stmt = stmt.where(Event.device_id == device_id)
        if partition_id:
            stmt = stmt.where(Event.partition_id == partition_id)
        if since:
            stmt = stmt.where(Event.received_at >= since)
        stmt = stmt.limit(limit).offset(offset)
        async with self.session() as session:
            return (await session.execute(stmt)).scalars().all()

    async def unacknowledged_alarms(self) -> Sequence[Event]:
        """Openstaande alarmen — de lijst die na een herstart de escalatie hervat."""
        stmt = (
            select(Event)
            .where(Event.severity == "alarm", Event.acknowledged_at.is_(None))
            .order_by(Event.received_at.desc())
        )
        async with self.session() as session:
            return (await session.execute(stmt)).scalars().all()

    async def acknowledge(self, event_id: int, by: str) -> Event | None:
        async with self.session() as session:
            row = await session.get(Event, event_id)
            if row is None or row.acknowledged_at is not None:
                return row
            row.acknowledged_at = utcnow()
            row.acknowledged_by = by
            await session.commit()
            return row

    async def acknowledge_all_alarms(self, by: str) -> int:
        async with self.session() as session:
            rows = (
                (
                    await session.execute(
                        select(Event).where(
                            Event.severity == "alarm", Event.acknowledged_at.is_(None)
                        )
                    )
                )
                .scalars()
                .all()
            )
            now = utcnow()
            for row in rows:
                row.acknowledged_at = now
                row.acknowledged_by = by
            await session.commit()
            return len(rows)

    # ── Logboek van meldingen ────────────────────────────────────────────────

    async def log_notification(
        self, event_id: int | None, channel: str, status: str, detail: str | None = None
    ) -> None:
        async with self.session() as session:
            session.add(
                NotificationLog(event_id=event_id, channel=channel, status=status, detail=detail)
            )
            await session.commit()

    async def log_call(
        self,
        event_id: int | None,
        attempt: int,
        variants: str,
        status: str,
        detail: str | None = None,
    ) -> None:
        async with self.session() as session:
            session.add(
                CallAttempt(
                    event_id=event_id,
                    attempt=attempt,
                    variants=variants,
                    status=status,
                    detail=detail,
                )
            )
            await session.commit()

    async def recent_failures(self, hours: int = 24) -> int:
        """Aantal mislukte meldingen, voor de waarschuwingsbalk op het dashboard."""
        since = utcnow() - timedelta(hours=hours)
        async with self.session() as session:
            notifications = (
                (
                    await session.execute(
                        select(NotificationLog).where(
                            NotificationLog.status == "failed",
                            NotificationLog.created_at >= since,
                        )
                    )
                )
                .scalars()
                .all()
            )
            calls = (
                (
                    await session.execute(
                        select(CallAttempt).where(
                            CallAttempt.status == "failed", CallAttempt.created_at >= since
                        )
                    )
                )
                .scalars()
                .all()
            )
            return len(notifications) + len(calls)

    # ── Status die een herstart moet overleven ───────────────────────────────

    async def get_state(self, key: str, default: Any = None) -> Any:
        async with self.session() as session:
            row = await session.get(StateEntry, key)
            if row is None:
                return default
            return json.loads(row.value)

    async def set_state(self, key: str, value: Any) -> None:
        payload = json.dumps(value)
        async with self.session() as session:
            row = await session.get(StateEntry, key)
            if row is None:
                session.add(StateEntry(key=key, value=payload, updated_at=utcnow()))
            else:
                row.value = payload
                row.updated_at = utcnow()
            await session.commit()

    # ── Onderhoud ────────────────────────────────────────────────────────────

    async def purge_old_events(self, retention_days: int) -> int:
        """Ruim oude events op. Alarmen blijven altijd staan.

        Een inbraak van drie jaar geleden wil je nog kunnen terugvinden; een
        hartslagbericht van vorige week niet.
        """
        if retention_days <= 0:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        async with self.session() as session:
            result = await session.execute(
                delete(Event).where(Event.received_at < cutoff, Event.severity != "alarm")
            )
            await session.commit()
            count = cast("CursorResult[Any]", result).rowcount or 0
        if count:
            _LOGGER.info("%d oude events opgeruimd (ouder dan %d dagen)", count, retention_days)
        return count
