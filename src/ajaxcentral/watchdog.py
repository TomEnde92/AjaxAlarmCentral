"""Supervisie: stilte van de hub is zelf een alarm.

Dit is de reden om een alarmcentrale in eigen beheer te draaien. Een inbreker
die de stroom eruit trekt of de netwerkkabel doorknipt, zorgt er juist voor dat
er géén inbraakmelding komt. Een centrale die alleen op binnenkomende alarmen
reageert, blijft dan stil — precies op het moment dat het ertoe doet.

Daarom draaien we het om: de hub hoort zich met een vast interval te melden, en
het uitblijven daarvan genereert zelf een alarm dat de hele belketen doorloopt.
"""

from __future__ import annotations

import asyncio
import logging

from .config import Config
from .normalize import internal_event
from .pipeline import EventPipeline
from .state import SystemState
from .tasks import cancel_task

_LOGGER = logging.getLogger(__name__)


class Watchdog:
    def __init__(
        self,
        config: Config,
        state: SystemState,
        pipeline: EventPipeline,
        *,
        check_interval: float = 10.0,
    ) -> None:
        self._config = config
        self._state = state
        self._pipeline = pipeline
        self._check_interval = check_interval
        self._task: asyncio.Task[None] | None = None
        #: Eigen vlag, los van SystemState.hub_online. Zodra er weer een
        #: bericht binnenkomt zet de receiver die status namelijk meteen op
        #: online, en dan zou de watchdog de overgang missen en nooit een
        #: herstelmelding sturen — terwijl je net wel een alarmoproep kreeg
        #: dat de hub weg was.
        self._offline_reported = False

    @property
    def threshold_seconds(self) -> float:
        return self._config.sia.offline_after_seconds

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="watchdog")
        _LOGGER.info(
            "Watchdog actief: alarm als de hub langer dan %.0f seconden zwijgt "
            "(ping-interval %ds x %.1f)",
            self.threshold_seconds,
            self._config.sia.ping_interval_seconds,
            self._config.sia.offline_factor,
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
                self.check()
            except Exception:  # pragma: no cover - defensief
                _LOGGER.exception("Watchdog-controle mislukt")

    def check(self) -> None:
        """Eén controleronde. Apart aanroepbaar, zodat de test niet hoeft te wachten."""
        stale = self._state.is_stale(self.threshold_seconds)

        if stale and not self._offline_reported:
            self._offline_reported = True
            self._state.mark_offline()
            seconds = self._state.seconds_since_contact
            detail = (
                f"laatste bericht {seconds:.0f} seconden geleden"
                if seconds is not None
                else "nog geen enkel bericht ontvangen sinds het opstarten"
            )
            _LOGGER.error("Hub niet bereikbaar: %s", detail)
            self._pipeline.submit(internal_event("HUBOFF", self._config, message=detail))
            return

        if stale:
            # Al gemeld; niet elke tien seconden opnieuw een alarm aanmaken.
            # De escalatie in de belketen zorgt voor de herhaling, tot je
            # het alarm in het dashboard bevestigt.
            return

        if self._offline_reported:
            self._offline_reported = False
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
