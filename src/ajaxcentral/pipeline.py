"""De verwerkingspijplijn: van binnengekomen event naar opgeslagen en verspreid.

Alles wat een event oplevert — de hub, de watchdog, de zelftest — levert het
hier af. Dat garandeert dat een zelfgemaakte melding exact dezelfde route
volgt als een echte inbraakmelding: opslaan, status bijwerken, verspreiden.

De inlevering is bewust niet-blokkerend. De SIA-callback draait in de lus die
ook de volgende berichten van de hub leest; die mag nooit wachten op een
databaseschrijfactie of een haperend Matrix-verzoek.
"""

from __future__ import annotations

import asyncio
import logging

from .bus import EventBus
from .config import Config
from .db import Database
from .models import AlarmEvent
from .state import SystemState
from .tasks import cancel_task

_LOGGER = logging.getLogger(__name__)


class EventPipeline:
    def __init__(
        self,
        config: Config,
        db: Database,
        bus: EventBus[AlarmEvent],
        state: SystemState,
        *,
        queue_size: int = 512,
    ) -> None:
        self._config = config
        self._db = db
        self._bus = bus
        self._state = state
        self._queue: asyncio.Queue[AlarmEvent] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None

    def submit(self, alarm: AlarmEvent) -> None:
        """Lever een event in. Mag vanuit elke context aangeroepen worden."""
        try:
            self._queue.put_nowait(alarm)
        except asyncio.QueueFull:  # pragma: no cover - vergt duizenden events
            _LOGGER.error("Verwerkingswachtrij vol; event %s is NIET verwerkt", alarm.summary())

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="pipeline")

    async def stop(self) -> None:
        if self._task is None:
            return
        # Eerst afmaken wat er nog in de rij staat: een alarm verliezen bij het
        # afsluiten is precies wat je niet wilt.
        try:
            await asyncio.wait_for(self._queue.join(), timeout=5)
        except TimeoutError:
            _LOGGER.warning(
                "Wachtrij niet leeg bij afsluiten; %d events blijven liggen", self._queue.qsize()
            )
        await cancel_task(self._task)
        self._task = None

    async def _run(self) -> None:
        while True:
            alarm = await self._queue.get()
            try:
                await self._process(alarm)
            except Exception:
                _LOGGER.exception("Verwerken van event mislukt: %s", alarm.summary())
            finally:
                self._queue.task_done()

    async def _process(self, alarm: AlarmEvent) -> None:
        # Eerst opslaan, dan pas verspreiden: abonnees krijgen zo een event
        # met een database-id, waarmee ze meldingen en belpogingen kunnen
        # koppelen. En mocht het proces hierna omvallen, dan staat het event
        # in elk geval in het logboek.
        await self._db.store_event(alarm)
        self._state.apply(alarm)
        if alarm.severity == "alarm":
            self._state.open_alarms = len(await self._db.unacknowledged_alarms())
        self._bus.publish(alarm)
        _LOGGER.info("[%s] %s", alarm.severity, alarm.summary())
