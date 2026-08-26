"""Startpunt: zet alle onderdelen op, draai ze, en sluit netjes af."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Iterator
from pathlib import Path

import uvicorn

from .bus import EventBus
from .config import Config, load_config
from .db import Database
from .models import AlarmEvent
from .mqtt import MqttPublisher
from .notify.base import NotifierRegistry
from .notify.dispatcher import NotificationDispatcher
from .notify.matrix.notifier import MatrixNotifier
from .pipeline import EventPipeline
from .receiver import Receiver
from .selftest import SelfTest
from .state import SystemState
from .tasks import cancel_task
from .watchdog import Watchdog
from .web.app import WebContext, create_app

_LOGGER = logging.getLogger("ajaxcentral")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # pysiaalarm logt elk binnengekomen frame op debug; dat is bij normaal
    # gebruik alleen maar ruis.
    logging.getLogger("pysiaalarm").setLevel(logging.WARNING)


class _QuietServer(uvicorn.Server):
    """Uvicorn-server die geen signalen afvangt.

    Standaard vervangt uvicorn tijdens `serve()` de SIGINT- en SIGTERM-handlers
    door die van zichzelf. Daarmee zou het afsluiten via de webserver lopen in
    plaats van via onze eigen route, en dan is niet gegarandeerd dat de
    ontvangst, de wachtrij en de database in de juiste volgorde worden
    afgesloten. Hier is de webserver maar één van de onderdelen, dus de
    afsluitvolgorde hoort in Application.stop() te staan.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class Application:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = Database(config.database.path)
        self.bus: EventBus[AlarmEvent] = EventBus()
        self.state = SystemState(config)
        self.pipeline = EventPipeline(config, self.db, self.bus, self.state)
        self.receiver = Receiver(config, self.pipeline.submit)
        self.watchdog = Watchdog(config, self.state, self.pipeline)
        self.registry = NotifierRegistry()
        self.matrix: MatrixNotifier | None = None
        self.selftest: SelfTest | None = None
        self.mqtt: MqttPublisher | None = None
        self.dispatcher: NotificationDispatcher | None = None
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task[None] | None = None

    # ── Opstarten ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self.db.connect()
        await self.db.purge_old_events(self.config.database.retention_days)

        # Status terughalen vóór er nieuwe events binnenkomen, anders zou een
        # binnenkomend bericht overschreven worden door de oude geschiedenis.
        await self.state.restore_from_db(self.db)

        if self.config.matrix.enabled:
            self.matrix = MatrixNotifier(self.config, self.db)
            await self.matrix.start()
            self.registry.register(self.matrix)
            self.selftest = SelfTest(self.config, self.db, self.matrix)
        else:
            _LOGGER.warning(
                "Matrix staat uit in config: er gaan GEEN meldingen uit en je "
                "telefoon gaat niet bij een alarm."
            )

        self.dispatcher = NotificationDispatcher(
            self.config, self.bus, self.registry, matrix=self.matrix
        )
        await self.dispatcher.start()

        if self.config.mqtt.enabled:
            self.mqtt = MqttPublisher(self.config, self.bus, self.state)
            await self.mqtt.start()

        await self.pipeline.start()
        await self.receiver.start()
        await self.watchdog.start()

        if self.selftest is not None:
            await self.selftest.start()

        # Alarmen die nog openstonden bij de vorige afsluiting krijgen opnieuw
        # een belronde. Een herstart mag een lopend alarm niet stilzetten.
        if self.matrix is not None:
            await self.matrix.escalation.resume_open_alarms()

        await self._start_web()
        _LOGGER.info("Alarmcentrale draait")

    async def _start_web(self) -> None:
        context = WebContext(
            config=self.config,
            db=self.db,
            bus=self.bus,
            state=self.state,
            selftest=self.selftest,
            receiver=self.receiver,
            matrix=self.matrix,
            on_acknowledge=(self.matrix.escalation.cancel_for if self.matrix is not None else None),
        )
        app = create_app(context)
        server_config = uvicorn.Config(
            app,
            host=self.config.web.host,
            port=self.config.web.port,
            log_level="warning",
            access_log=False,
        )
        self._server = _QuietServer(server_config)
        self._server_task = asyncio.create_task(self._server.serve(), name="web")
        _LOGGER.info("Dashboard op http://%s:%s", self.config.web.host, self.config.web.port)

    # ── Afsluiten ────────────────────────────────────────────────────────────

    async def stop(self) -> None:
        _LOGGER.info("Afsluiten…")
        if self._server is not None and self._server_task is not None:
            # Uvicorn eerst zelf laten afronden: direct cancellen breekt zijn
            # lifespan-taak af en levert een misleidende ERROR-traceback op bij
            # een verder volstrekt normale stop.
            self._server.should_exit = True
            try:
                await asyncio.wait_for(asyncio.shield(self._server_task), timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                await cancel_task(self._server_task)
        self._server_task = None

        await self.watchdog.stop()
        await self.receiver.stop()
        if self.selftest is not None:
            await self.selftest.stop()
        if self.mqtt is not None:
            await self.mqtt.stop()
        # De pijplijn als laatste van de verwerkers: die maakt de wachtrij leeg
        # zodat een net binnengekomen event nog wordt opgeslagen.
        await self.pipeline.stop()
        if self.dispatcher is not None:
            await self.dispatcher.stop()
        await self.registry.stop_all()
        await self.db.close()
        _LOGGER.info("Gestopt")


async def _run(config: Config) -> None:
    app = Application(config)
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover - niet op alle platforms
            signal.signal(sig, lambda *_: stop_event.set())

    await app.start()
    try:
        await stop_event.wait()
    finally:
        await app.stop()


def run() -> int:
    setup_logging(os.environ.get("AJAXCENTRAL_LOG_LEVEL", "INFO"))
    try:
        config = load_config()
    except Exception as exc:
        _LOGGER.error("Configuratie deugt niet: %s", exc)
        return 2

    if not Path("config.yaml").exists():
        _LOGGER.warning(
            "Geen config.yaml gevonden; de standaardwaarden worden gebruikt. "
            "Kopieer config.example.yaml naar config.yaml om dit aan te passen."
        )

    with contextlib.suppress(KeyboardInterrupt):  # Ctrl-C is een normale stop
        asyncio.run(_run(config))
    return 0


if __name__ == "__main__":
    sys.exit(run())
