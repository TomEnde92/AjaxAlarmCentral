"""In-process publish/subscribe voor alarmevents.

Bewuste keuze voor een eigen mini-bus in plaats van een externe broker: het
volume is een handvol events per dag en alle abonnees draaien in hetzelfde
proces. Een trage abonnee (bijvoorbeeld Matrix met een haperend netwerk) mag
de ontvangst van nieuwe hub-events nooit blokkeren, dus elke abonnee heeft een
eigen begrensde wachtrij.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Generic, TypeVar

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


class EventBus(Generic[T]):
    """Fan-out van events naar alle abonnees, zonder dat één abonnee kan blokkeren."""

    def __init__(self, queue_size: int = 256) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[T]] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: T) -> None:
        """Stuur een event naar alle abonnees.

        Synchroon en niet-blokkerend, zodat de SIA-callback direct de ACK kan
        afhandelen. Zit een wachtrij vol, dan valt het oudste event eruit: bij
        een volle rij is een verse alarmmelding waardevoller dan een oude.
        """
        for queue in self._subscribers:
            if queue.full():
                try:
                    dropped = queue.get_nowait()
                    _LOGGER.warning(
                        "Wachtrij van een abonnee zit vol, oudste event weggegooid: %r",
                        dropped,
                    )
                except asyncio.QueueEmpty:  # pragma: no cover - race, praktisch onbereikbaar
                    pass
            queue.put_nowait(event)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[T]]:
        """Abonneer voor de duur van het contextblok."""
        queue: asyncio.Queue[T] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    @asynccontextmanager
    async def stream(self) -> AsyncIterator[AsyncIterator[T]]:
        """Zelfde als subscribe(), maar levert direct een async iterator op."""

        async def _iterate(queue: asyncio.Queue[T]) -> AsyncIterator[T]:
            while True:
                yield await queue.get()

        async with self.subscribe() as queue:
            yield _iterate(queue)
