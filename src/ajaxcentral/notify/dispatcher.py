"""Koppelt de eventstroom aan de meldkanalen.

De dispatcher luistert op de bus en beslist per event: alleen loggen, een
bericht sturen, of bellen. Het versturen gebeurt in losse taken, zodat een
haperende homeserver de verwerking van volgende events niet ophoudt — precies
op het moment dat er meerdere melders tegelijk afgaan wil je dat niet.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from ..bus import EventBus
from ..config import Config
from ..models import AlarmEvent
from ..tasks import cancel_task
from .base import Notifier, NotifierRegistry, NotifyError
from .matrix.notifier import MatrixNotifier
from .rules import NotificationRules

_LOGGER = logging.getLogger(__name__)


class NotificationDispatcher:
    def __init__(
        self,
        config: Config,
        bus: EventBus[AlarmEvent],
        registry: NotifierRegistry,
        *,
        matrix: MatrixNotifier | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._registry = registry
        self._matrix = matrix
        self._rules = NotificationRules(config)
        self._task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()

    @property
    def rules(self) -> NotificationRules:
        return self._rules

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="dispatcher")

    async def stop(self) -> None:
        await cancel_task(self._task)
        self._task = None
        for task in list(self._inflight):
            await cancel_task(task)
        self._inflight.clear()

    async def _run(self) -> None:
        async with self._bus.subscribe() as queue:
            while True:
                alarm = await queue.get()
                try:
                    self._handle(alarm)
                except Exception:  # pragma: no cover - defensief
                    _LOGGER.exception("Melden van %s mislukte", alarm.summary())

    def _handle(self, alarm: AlarmEvent) -> None:
        if self._rules.should_notify(alarm):
            for notifier in self._registry.notifiers:
                self._spawn(self._send(notifier, alarm))

        if self._matrix is not None and self._rules.should_ring(alarm):
            _LOGGER.warning("Belronde gestart voor: %s", alarm.summary())
            self._spawn(self._matrix.ring_for(alarm))

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Start een verzendactie los van de eventstroom.

        Een verwijzing vasthouden is nodig: zonder harde referentie kan de
        garbage collector een lopende taak weggooien, en dan verdwijnt er
        stilletjes een alarmmelding.
        """
        task = asyncio.ensure_future(coro)
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    @staticmethod
    async def _send(notifier: Notifier, alarm: AlarmEvent) -> None:
        try:
            await notifier.send_event(alarm)
        except NotifyError as exc:
            # Al vastgelegd in NotificationLog; het dashboard toont de fout.
            _LOGGER.error("Melding via %s mislukte: %s", notifier.name, exc)
        except Exception:
            _LOGGER.exception("Onverwachte fout bij melden via %s", notifier.name)
