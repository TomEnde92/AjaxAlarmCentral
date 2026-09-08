"""Supervisie: stilte van de hub is zelf een alarm.

Dit is de reden om een alarmcentrale in eigen beheer te draaien. Een inbreker
die de stroom eruit trekt of de netwerkkabel doorknipt, zorgt er juist voor dat
er géén inbraakmelding komt. Een centrale die alleen op binnenkomende alarmen
reageert, blijft dan stil — precies op het moment dat het ertoe doet.

Daarom draaien we het om: de hub hoort zich met een vast interval te melden, en
het uitblijven daarvan genereert zelf een alarm dat de hele belketen doorloopt.
En zolang de hub weg blijft en niemand dat alarm bevestigt, wordt het herhaald:
één gemiste belronde mag niet betekenen dat de uitval daarna dagen onopgemerkt
blijft.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from .config import HUB_CLOCK_LIMIT_SECONDS, Config
from .db import Database
from .models import AlarmEvent, utcnow
from .normalize import internal_event
from .pipeline import EventPipeline
from .state import SystemState
from .tasks import cancel_task

_LOGGER = logging.getLogger(__name__)

#: Hoe vaak een wegdrijvende hubklok opnieuw gemeld wordt zolang hij niet is
#: rechtgezet. Dagelijks: vaker is zeuren, minder vaak is het vergeten.
_CLOCK_REPEAT_SECONDS = 24 * 3600

#: Zoveel seconden onder de drempel moet de klok zakken voordat we hem hersteld
#: noemen. Zonder die marge wipt de melding heen en weer rond de grens.
_CLOCK_HYSTERESIS = 5.0


class Watchdog:
    def __init__(
        self,
        config: Config,
        state: SystemState,
        pipeline: EventPipeline,
        *,
        db: Database | None = None,
        check_interval: float = 10.0,
    ) -> None:
        self._config = config
        self._state = state
        self._pipeline = pipeline
        self._db = db
        self._check_interval = check_interval
        self._task: asyncio.Task[None] | None = None
        #: Eigen vlag, los van SystemState.hub_online. Zodra er weer een
        #: bericht binnenkomt zet de receiver die status namelijk meteen op
        #: online, en dan zou de watchdog de overgang missen en nooit een
        #: herstelmelding sturen — terwijl je net wel een alarmoproep kreeg
        #: dat de hub weg was.
        self._offline_reported = False
        #: Het laatst ingediende HUBOFF-event en wanneer. De pijplijn zet er
        #: na opslag de database-id op, waarmee we kunnen zien of iemand het
        #: in het dashboard bevestigd heeft.
        self._last_report: AlarmEvent | None = None
        self._last_report_at: datetime | None = None
        self._repeats = 0
        #: Wanneer we voor het laatst over de klok van de hub gewaarschuwd
        #: hebben. None = de klok liep bij de laatste controle gelijk.
        self._clock_reported_at: datetime | None = None

    @property
    def threshold_seconds(self) -> float:
        return self._config.sia.offline_after_seconds

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="watchdog")
        repeat = self._config.sia.offline_repeat_seconds
        _LOGGER.info(
            "Watchdog actief: alarm als de hub langer dan %.0f seconden zwijgt "
            "(ping-interval %ds x %.1f), %s",
            self.threshold_seconds,
            self._config.sia.ping_interval_seconds,
            self._config.sia.offline_factor,
            f"herhaald elke {repeat}s tot bevestiging" if repeat > 0 else "zonder herhaling",
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        await cancel_task(self._task)
        self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._check_interval)
            try:
                await self.check()
            except Exception:  # pragma: no cover - defensief
                _LOGGER.exception("Watchdog-controle mislukt")

    async def check(self) -> None:
        """Eén controleronde. Apart aanroepbaar, zodat de test niet hoeft te wachten."""
        self._check_clock()
        stale = self._state.is_stale(self.threshold_seconds)

        if stale and not self._offline_reported:
            self._offline_reported = True
            self._repeats = 0
            self._state.mark_offline()
            seconds = self._state.seconds_since_contact
            detail = (
                f"laatste bericht {seconds:.0f} seconden geleden"
                if seconds is not None
                else "nog geen enkel bericht ontvangen sinds het opstarten"
            )
            _LOGGER.error("Hub niet bereikbaar: %s", detail)
            self._report(internal_event("HUBOFF", self._config, message=detail))
            return

        if stale:
            # Al gemeld. De escalatie belt een vast aantal keer en stopt dan;
            # blijft de hub weg en heeft niemand het alarm bevestigd, dan
            # melden we opnieuw zodat er een nieuwe belronde start.
            if await self._should_repeat():
                self._repeats += 1
                seconds = self._state.seconds_since_contact
                detail = (
                    f"nog steeds geen contact, laatste bericht {seconds / 60:.0f} minuten "
                    f"geleden (herhaling {self._repeats})"
                    if seconds is not None
                    else f"nog steeds geen enkel bericht sinds het opstarten "
                    f"(herhaling {self._repeats})"
                )
                _LOGGER.error("Hub nog steeds niet bereikbaar: %s", detail)
                self._report(internal_event("HUBOFF", self._config, message=detail))
            return

        if self._offline_reported:
            self._offline_reported = False
            self._last_report = None
            self._last_report_at = None
            seconds = self._state.seconds_since_contact
            _LOGGER.info("Hub weer bereikbaar")
            self._pipeline.submit(
                internal_event(
                    "HUBON",
                    self._config,
                    message=(
                        f"weer contact, laatste bericht {seconds:.0f} seconden geleden"
                        if seconds is not None
                        else "weer contact"
                    ),
                )
            )

    def _check_clock(self) -> None:
        """Waarschuw voordat de klok van de hub de centrale doof maakt.

        Een hub die zijn tijd niet bijhoudt loopt elke dag een paar seconden
        verder achter. Zolang dat binnen de marge van het protocol blijft merkt
        niemand er iets van; daarboven wordt élk bericht geweigerd. De centrale
        slaat dan wel alarm omdat de hub "stil" is, maar dan ben je al doof.

        Daarom melden we het als storing zodra de afwijking richting de grens
        loopt, met het aantal seconden erbij: dan kun je de klok van de hub
        gelijkzetten voordat het zover is. Zolang het niet opgelost is wordt de
        melding dagelijks herhaald — één bericht dat je in de auto wegveegt,
        mag niet het enige zijn dat je erover hoort.
        """
        threshold = self._config.sia.clock_warn_seconds
        offset = self._state.clock_offset_seconds
        if threshold <= 0 or offset is None:
            return

        if offset >= threshold:
            if self._clock_reported_at is not None and (
                (utcnow() - self._clock_reported_at).total_seconds() < _CLOCK_REPEAT_SECONDS
            ):
                return
            self._clock_reported_at = utcnow()
            detail = (
                f"de klok van de hub loopt {offset:.0f} seconden achter; "
                f"vanaf {HUB_CLOCK_LIMIT_SECONDS:.0f} seconden weigert de centrale "
                "elk bericht van de hub. Zet de tijd van de hub gelijk in de Ajax-app "
                "of herstart hem."
            )
            _LOGGER.error("Klok van de hub loopt uit de pas: %s", detail)
            self._pipeline.submit(internal_event("CLOCKOFF", self._config, message=detail))
            return

        # Marge terug omlaag: pas herstellen als het echt weer goed is, anders
        # wipt de melding heen en weer rond de drempel.
        if self._clock_reported_at is not None and offset < threshold - _CLOCK_HYSTERESIS:
            self._clock_reported_at = None
            _LOGGER.info("Klok van de hub loopt weer gelijk (%.0f s)", offset)
            self._pipeline.submit(
                internal_event(
                    "CLOCKOK",
                    self._config,
                    message=f"verschil terug op {offset:.0f} seconden",
                )
            )

    def _report(self, alarm: AlarmEvent) -> None:
        self._last_report = alarm
        self._last_report_at = utcnow()
        self._pipeline.submit(alarm)

    async def _should_repeat(self) -> bool:
        interval = self._config.sia.offline_repeat_seconds
        if interval <= 0 or self._last_report_at is None:
            return False
        if (utcnow() - self._last_report_at).total_seconds() < interval:
            return False
        return not await self._acknowledged(self._last_report)

    async def _acknowledged(self, alarm: AlarmEvent | None) -> bool:
        """Bevestigd in het dashboard? Zonder database (of id) nemen we van niet aan."""
        if self._db is None or alarm is None or alarm.db_id is None:
            return False
        try:
            row = await self._db.get_event(alarm.db_id)
        except Exception:
            _LOGGER.exception("Bevestiging van HUBOFF niet te controleren")
            return False
        return row is not None and row.acknowledged_at is not None
