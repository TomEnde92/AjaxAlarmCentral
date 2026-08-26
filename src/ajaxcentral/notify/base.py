"""Kanaalonafhankelijke basis voor meldingen.

Er is nu één kanaal (Matrix), maar de indeling is bewust een registry. Wie
Android gebruikt zonder tweede kanaal, hangt namelijk volledig aan één
push-pad; een tweede kanaal toevoegen moet daarom een kwestie zijn van een
klasse plus een configblok, niet van een verbouwing.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from ..models import AlarmEvent

_LOGGER = logging.getLogger(__name__)


class NotifyError(Exception):
    """Een melding kon niet worden afgeleverd."""


@runtime_checkable
class Notifier(Protocol):
    """Eén meldkanaal."""

    name: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send_event(self, alarm: AlarmEvent) -> None:
        """Stuur een gewone melding over dit event."""
        ...


class NotifierRegistry:
    def __init__(self) -> None:
        self._notifiers: list[Notifier] = []

    def register(self, notifier: Notifier) -> None:
        self._notifiers.append(notifier)
        _LOGGER.info("Meldkanaal geregistreerd: %s", notifier.name)

    def __iter__(self) -> object:
        return iter(self._notifiers)

    def __len__(self) -> int:
        return len(self._notifiers)

    @property
    def notifiers(self) -> list[Notifier]:
        return list(self._notifiers)

    async def start_all(self) -> None:
        for notifier in self._notifiers:
            await notifier.start()

    async def stop_all(self) -> None:
        for notifier in self._notifiers:
            try:
                await notifier.stop()
            except Exception:  # pragma: no cover - afsluiten mag nooit crashen
                _LOGGER.exception("Kanaal %s afsluiten mislukte", notifier.name)
