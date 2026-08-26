"""Blijven bellen tot je bevestigt.

Eén oproep is geen alarmering. Je slaapt, je telefoon ligt in een andere kamer,
of de push komt niet aan — en op Android is dat laatste een bekend probleem.
Daarom belt de centrale door met een vast interval tot je het alarm in het
dashboard bevestigt, of tot het maximum aantal pogingen bereikt is.

Lopende escalaties worden bij het opstarten hervat op basis van de openstaande
alarmen in de database. Een herstart of stroomdip mag een alarm niet stilzetten.
"""

from __future__ import annotations

import asyncio
import logging

from ...config import Config
from ...db import Database
from ...models import AlarmEvent, as_utc
from ...tasks import cancel_task
from .ring import RingSender

_LOGGER = logging.getLogger(__name__)


class EscalationManager:
    def __init__(self, config: Config, db: Database, ring_sender: RingSender) -> None:
        self._config = config
        self._db = db
        self._ring = ring_sender
        self._tasks: dict[int, asyncio.Task[None]] = {}

    @property
    def active_event_ids(self) -> list[int]:
        return sorted(self._tasks)

    # ── Levenscyclus ─────────────────────────────────────────────────────────

    async def start_for(self, alarm: AlarmEvent) -> None:
        """Begin te bellen voor dit alarm."""
        if alarm.db_id is None:
            _LOGGER.error("Alarm zonder database-id; escalatie overgeslagen")
            return
        if alarm.db_id in self._tasks:
            return
        task = asyncio.create_task(self._escalate(alarm), name=f"escalatie-{alarm.db_id}")
        self._tasks[alarm.db_id] = task

    async def resume_open_alarms(self) -> int:
        """Hervat het bellen voor alarmen die nog openstaan na een herstart."""
        resumed = 0
        for row in await self._db.unacknowledged_alarms():
            if row.severity != "alarm":
                continue
            already = len([c for c in row.calls if c.status == "sent"])
            if already >= self._config.matrix.ring.max_attempts:
                continue
            alarm = AlarmEvent(
                code=row.code,
                category=row.category,
                severity=row.severity,
                title=row.title,
                description=row.description,
                source=row.source,
                device_id=row.device_id,
                device_name=row.device_name,
                partition_id=row.partition_id,
                partition_name=row.partition_name,
                user_id=row.user_id,
                user_name=row.user_name,
                received_at=as_utc(row.received_at),
                event_at=as_utc(row.event_at),
                uid=row.uid,
                db_id=row.id,
            )
            await self.start_for(alarm)
            resumed += 1
        if resumed:
            _LOGGER.warning(
                "%d openstaand alarm(en) hervat na herstart; er wordt opnieuw gebeld",
                resumed,
            )
        return resumed

    def cancel_for(self, event_id: int) -> None:
        """Stop het bellen; aangeroepen zodra je bevestigt."""
        task = self._tasks.pop(event_id, None)
        if task is not None:
            task.cancel()
            _LOGGER.info("Escalatie voor event %d gestopt na bevestiging", event_id)

    async def stop(self) -> None:
        for task in list(self._tasks.values()):
            await cancel_task(task)
        self._tasks.clear()

    # ── De belronde ──────────────────────────────────────────────────────────

    async def _escalate(self, alarm: AlarmEvent) -> None:
        ring = self._config.matrix.ring
        event_id = alarm.db_id
        assert event_id is not None

        try:
            for attempt in range(1, ring.max_attempts + 1):
                if await self._is_acknowledged(event_id):
                    _LOGGER.info(
                        "Alarm %d bevestigd; escalatie na %d pogingen gestopt",
                        event_id,
                        attempt - 1,
                    )
                    break

                result = await self._ring.ring(
                    f"{alarm.summary()} (poging {attempt}/{ring.max_attempts})",
                    call_id=f"{alarm.uid[:16]}{attempt:02d}",
                )
                await self._db.log_call(
                    event_id,
                    attempt,
                    ",".join(result.sent) or "-",
                    "sent" if result.ok else "failed",
                    result.describe(),
                )

                if attempt < ring.max_attempts:
                    await asyncio.sleep(ring.retry_interval_seconds)
            else:
                _LOGGER.error(
                    "Alarm %d is na %d belpogingen nog steeds niet bevestigd: %s",
                    event_id,
                    ring.max_attempts,
                    alarm.summary(),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Escalatie voor event %d liep vast", event_id)
        finally:
            self._tasks.pop(event_id, None)
            # Ruim de call-sessie op, anders blijft er een gesprek hangen in de
            # room dat niemand kan beantwoorden.
            try:
                await self._ring.clear_membership()
            except Exception:  # pragma: no cover - opruimen mag nooit crashen
                _LOGGER.debug("Opruimen van het lidmaatschap mislukte", exc_info=True)

    async def _is_acknowledged(self, event_id: int) -> bool:
        row = await self._db.get_event(event_id)
        return row is not None and row.acknowledged_at is not None
