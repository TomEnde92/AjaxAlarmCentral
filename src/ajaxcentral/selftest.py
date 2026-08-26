"""Periodieke testoproep: bewijs dat de keten tot je telefoon nog werkt.

Dit is er omdat de belketen op Android stil kan sneuvelen. Een verlopen token,
een gewijzigde room, een push-gateway die je toestel niet meer kent — je merkt
er niets van, want er gebeurt precies hetzelfde als wanneer alles in orde is:
niets. Tot er een keer echt wordt ingebroken.

Daarom belt de centrale zichzelf op een vast moment. Bevestig je die testring
niet binnen de deadline, dan zet het dashboard een waarschuwing. Zo ontdek je
een kapot belpad op een dinsdagmiddag in plaats van tijdens een inbraak.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from .config import Config
from .db import Database
from .models import SelftestRun, as_utc, utcnow
from .notify.matrix.notifier import MatrixNotifier
from .tasks import cancel_task

_LOGGER = logging.getLogger(__name__)

_CHECK_INTERVAL = 60.0


class SelfTest:
    def __init__(self, config: Config, db: Database, matrix: MatrixNotifier | None) -> None:
        self._config = config
        self._db = db
        self._matrix = matrix
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if not self._config.selftest.enabled or self._matrix is None:
            return
        self._task = asyncio.create_task(self._run(), name="zelftest")
        _LOGGER.info(
            "Wekelijkse testoproep staat aan: %s om %02d:%02d",
            _WEEKDAYS[self._config.selftest.weekday],
            self._config.selftest.hour,
            self._config.selftest.minute,
        )

    async def stop(self) -> None:
        await cancel_task(self._task)
        self._task = None

    # ── Planning ─────────────────────────────────────────────────────────────

    def next_run_after(self, moment: datetime) -> datetime:
        """Eerstvolgende geplande testmoment ná dit tijdstip, in lokale tijd."""
        settings = self._config.selftest
        local = self._config.to_local(moment)
        candidate = local.replace(
            hour=settings.hour, minute=settings.minute, second=0, microsecond=0
        )
        days_ahead = (settings.weekday - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= local:
            candidate += timedelta(days=7)
        return candidate

    async def _run(self) -> None:
        while True:
            now = utcnow()
            target = self.next_run_after(now)
            wait = (target - self._config.to_local(now)).total_seconds()
            _LOGGER.debug("Volgende testoproep over %.0f seconden", wait)

            # In stukjes wachten in plaats van één lange sleep: zo overleeft de
            # planning een verzette systeemklok of een pauzerende Pi.
            while wait > 0:
                await asyncio.sleep(min(wait, _CHECK_INTERVAL))
                wait = (target - self._config.to_local(utcnow())).total_seconds()

            try:
                await self.run_once(kind="scheduled")
            except Exception:  # pragma: no cover - defensief
                _LOGGER.exception("Geplande testoproep mislukte")
            await self._check_overdue()

    # ── Uitvoeren ────────────────────────────────────────────────────────────

    async def run_once(self, kind: str = "manual") -> SelftestRun:
        """Voer één testoproep uit en leg het resultaat vast."""
        assert self._matrix is not None
        run = SelftestRun(kind=kind, ring_status="pending")
        async with self._db.session() as session:
            session.add(run)
            await session.commit()

        ok = await self._matrix.test_ring(
            "Testoproep van de alarmcentrale — bevestig hem in het dashboard"
        )

        async with self._db.session() as session:
            stored = await session.get(SelftestRun, run.id)
            if stored is not None:
                stored.ring_status = "sent" if ok else "failed"
                stored.detail = (
                    "Oproep verstuurd; wacht op bevestiging"
                    if ok
                    else "Oproep kon niet verstuurd worden"
                )
                await session.commit()
                run = stored

        if ok:
            _LOGGER.info("Testoproep verstuurd (%s)", kind)
        else:
            _LOGGER.error("Testoproep kon NIET verstuurd worden (%s)", kind)
        return run

    async def acknowledge_latest(self, by: str) -> SelftestRun | None:
        """Bevestig de laatste testoproep: bewijs dat je telefoon echt ging."""
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(SelftestRun).order_by(SelftestRun.started_at.desc()).limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            row.acknowledged_at = utcnow()
            row.detail = f"Bevestigd door {by}"
            await session.commit()
            _LOGGER.info("Testoproep bevestigd door %s", by)
            return row

    async def latest(self) -> SelftestRun | None:
        async with self._db.session() as session:
            return (
                await session.execute(
                    select(SelftestRun).order_by(SelftestRun.started_at.desc()).limit(1)
                )
            ).scalar_one_or_none()

    async def status(self) -> dict[str, Any]:
        """Wat het dashboard toont over de betrouwbaarheid van het belpad."""
        run = await self.latest()
        if run is None:
            return {
                "enabled": self._config.selftest.enabled,
                "state": "nooit uitgevoerd",
                "warning": self._config.selftest.enabled,
                "last": None,
            }

        deadline = timedelta(minutes=self._config.selftest.ack_deadline_minutes)
        overdue = run.acknowledged_at is None and utcnow() - as_utc(run.started_at) > deadline
        if run.ring_status == "failed":
            state, warning = "de testoproep kon niet verstuurd worden", True
        elif overdue:
            state, warning = "de testoproep is niet bevestigd", True
        elif run.acknowledged_at is not None:
            state, warning = "bevestigd, het belpad werkt", False
        else:
            state, warning = "verstuurd, wacht op bevestiging", False

        return {
            "enabled": self._config.selftest.enabled,
            "state": state,
            "warning": warning,
            "last": run.to_dict(),
        }

    async def _check_overdue(self) -> None:
        status = await self.status()
        if status.get("warning"):
            _LOGGER.error(
                "Belpad verdacht: %s. Controleer je Matrix-instellingen en "
                "push-regel voordat je hierop vertrouwt.",
                status.get("state"),
            )


_WEEKDAYS = (
    "maandag",
    "dinsdag",
    "woensdag",
    "donderdag",
    "vrijdag",
    "zaterdag",
    "zondag",
)
