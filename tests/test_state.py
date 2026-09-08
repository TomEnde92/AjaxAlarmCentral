"""Afgeleide status en de watchdog."""

from __future__ import annotations

from datetime import timedelta

from ajaxcentral.config import Config
from ajaxcentral.db import Database
from ajaxcentral.models import AlarmEvent, utcnow
from ajaxcentral.pipeline import EventPipeline
from ajaxcentral.state import SystemState
from ajaxcentral.watchdog import Watchdog


def _event(code: str, severity: str, category: str, **kwargs: object) -> AlarmEvent:
    return AlarmEvent(
        code=code,
        category=category,
        severity=severity,
        title=code,
        description="",
        **kwargs,  # type: ignore[arg-type]
    )


def test_in_en_uitschakelen(config: Config) -> None:
    state = SystemState(config)
    state.apply(_event("CL", "info", "arming", partition_id="1"))
    assert state.partitions["1"].armed
    state.apply(_event("OP", "info", "arming", partition_id="1"))
    assert not state.partitions["1"].armed


def test_nachtstand_telt_als_ingeschakeld(config: Config) -> None:
    state = SystemState(config)
    state.apply(_event("NL", "info", "arming", partition_id="1"))
    assert state.partitions["1"].armed


def test_volledig_inschakelen_zet_alle_groepen_aan(config: Config) -> None:
    """CL is "het huis staat aan", niet "groep 1 staat aan".

    De hub stuurt bij een volledige inschakeling één CL met het groepsveld op
    1. Wie dat als groepsbericht leest, laat de verdieping op het dashboard
    als uitgeschakeld staan terwijl die wél bewaakt wordt.
    """
    state = SystemState(config)
    state.apply(_event("CL", "info", "arming", partition_id="1"))
    assert all(partition.armed for partition in state.partitions.values())

    state.apply(_event("OP", "info", "arming", partition_id="1"))
    assert not any(partition.armed for partition in state.partitions.values())


def test_deelinschakeling_raakt_alleen_de_eigen_groep(config: Config) -> None:
    """Bij een deel- of nachtinschakeling is het groepsveld wél de groep."""
    state = SystemState(config)
    state.apply(_event("NL", "info", "arming", partition_id="1"))
    assert state.partitions["1"].armed
    assert not state.partitions["2"].armed


def test_onbekende_groep_komt_erbij(config: Config) -> None:
    """Een groep die niet in de configuratie staat mag niet verdwijnen."""
    state = SystemState(config)
    state.apply(_event("CG", "info", "arming", partition_id="7"))
    assert state.partitions["7"].armed
    assert not state.partitions["1"].armed


def test_storing_blijft_staan_tot_herstel(config: Config) -> None:
    state = SystemState(config)
    state.apply(_event("XT", "trouble", "battery", device_id="03"))
    assert len(state.troubles) == 1
    state.apply(_event("XR", "restore", "battery", device_id="03"))
    assert not state.troubles


def test_herstel_van_ander_apparaat_ruimt_niets_op(config: Config) -> None:
    """Twee melders met dezelfde storing zijn twee storingen."""
    state = SystemState(config)
    state.apply(_event("XT", "trouble", "battery", device_id="03"))
    state.apply(_event("XT", "trouble", "battery", device_id="04"))
    assert len(state.troubles) == 2
    state.apply(_event("XR", "restore", "battery", device_id="03"))
    assert len(state.troubles) == 1


def test_stilte_is_verdacht(config: Config) -> None:
    state = SystemState(config)
    state.note_contact()
    assert not state.is_stale(10)
    state.last_contact = utcnow() - timedelta(seconds=30)
    assert state.is_stale(10)


def test_stilte_telt_ook_zonder_ooit_contact(config: Config) -> None:
    """Een centrale die naast een dode hub opstart moet ook alarm slaan."""
    state = SystemState(config)
    state.started_at = utcnow() - timedelta(seconds=300)
    assert state.last_contact is None
    assert state.is_stale(10)


def test_klokverschil_van_de_hub_wordt_bijgehouden(config: Config) -> None:
    """Het verschil tussen hubtijd en ontvangst is de maat voor de klok."""
    state = SystemState(config)
    moment = utcnow()
    for _ in range(20):
        state.apply(
            _event(
                "RP",
                "heartbeat",
                "test",
                source="hub",
                event_at=moment - timedelta(seconds=30),
                received_at=moment,
            )
        )
    assert state.clock_offset_seconds is not None
    assert 29 < state.clock_offset_seconds < 31


async def test_watchdog_waarschuwt_voor_de_wegdrijvende_hubklok(
    config: Config, db: Database
) -> None:
    """Boven de protocolgrens weigert de centrale alles; dat hoort ze te zien aankomen."""
    from ajaxcentral.bus import EventBus

    bus: EventBus[AlarmEvent] = EventBus()
    state = SystemState(config)
    pipeline = EventPipeline(config, db, bus, state)
    watchdog = Watchdog(config, state, pipeline)
    state.note_contact()

    # Ruim binnen de marge: niets aan de hand.
    state.clock_offset_seconds = 3.0
    await watchdog.check()
    assert pipeline._queue.qsize() == 0

    # Over de drempel: één storing, en niet elke ronde opnieuw.
    state.clock_offset_seconds = config.sia.clock_warn_seconds + 1
    await watchdog.check()
    await watchdog.check()
    assert pipeline._queue.qsize() == 1
    warning = pipeline._queue.get_nowait()
    assert warning.code == "CLOCKOFF"
    assert warning.severity == "trouble"
    assert "40" in (warning.message or "")

    # Klok rechtgezet: herstelmelding, en daarna weer stil.
    state.clock_offset_seconds = 1.0
    await watchdog.check()
    await watchdog.check()
    assert pipeline._queue.qsize() == 1
    assert pipeline._queue.get_nowait().code == "CLOCKOK"


async def test_watchdog_meldt_uitval_en_herstel(config: Config, db: Database) -> None:
    from ajaxcentral.bus import EventBus

    bus: EventBus[AlarmEvent] = EventBus()
    state = SystemState(config)
    pipeline = EventPipeline(config, db, bus, state)
    watchdog = Watchdog(config, state, pipeline)

    state.started_at = utcnow() - timedelta(seconds=300)

    await watchdog.check()
    await watchdog.check()  # tweede ronde mag geen tweede alarm opleveren
    assert pipeline._queue.qsize() == 1
    first = pipeline._queue.get_nowait()
    assert first.code == "HUBOFF"
    assert first.severity == "alarm"

    state.note_contact()
    await watchdog.check()
    await watchdog.check()
    assert pipeline._queue.qsize() == 1
    assert pipeline._queue.get_nowait().code == "HUBON"


async def test_watchdog_herhaalt_tot_bevestiging(config: Config, db: Database) -> None:
    """Eén gemiste belronde mag niet betekenen dat de uitval daarna stil blijft."""
    from ajaxcentral.bus import EventBus

    bus: EventBus[AlarmEvent] = EventBus()
    state = SystemState(config)
    pipeline = EventPipeline(config, db, bus, state)
    config.sia.offline_repeat_seconds = 60
    watchdog = Watchdog(config, state, pipeline, db=db)
    state.started_at = utcnow() - timedelta(seconds=300)

    await watchdog.check()
    first = pipeline._queue.get_nowait()
    assert first.code == "HUBOFF"
    await db.store_event(first)  # wat de pijplijn normaal doet

    # Binnen het interval: niets.
    await watchdog.check()
    assert pipeline._queue.qsize() == 0

    # Interval verstreken, niet bevestigd: opnieuw melden.
    watchdog._last_report_at = utcnow() - timedelta(seconds=61)
    await watchdog.check()
    repeat = pipeline._queue.get_nowait()
    assert repeat.code == "HUBOFF"
    assert "herhaling 1" in (repeat.message or "")
    await db.store_event(repeat)

    # Bevestigd in het dashboard: stil, ook al is het interval verstreken.
    assert repeat.db_id is not None
    await db.acknowledge(repeat.db_id, "tom")
    watchdog._last_report_at = utcnow() - timedelta(seconds=61)
    await watchdog.check()
    assert pipeline._queue.qsize() == 0

    # Hub terug: herstelmelding, en de teller begint bij een volgende uitval opnieuw.
    state.note_contact()
    await watchdog.check()
    assert pipeline._queue.get_nowait().code == "HUBON"


async def test_pijplijn_verspreidt_ook_als_opslaan_faalt(config: Config) -> None:
    """Een volle SD-kaart mag geen stille alarmcentrale opleveren."""
    from ajaxcentral.bus import EventBus

    class KapotteDatabase:
        async def store_event(self, alarm: AlarmEvent) -> int:
            raise OSError("read-only file system")

        async def unacknowledged_alarms(self) -> list[object]:
            return []

    bus: EventBus[AlarmEvent] = EventBus()
    state = SystemState(config)
    pipeline = EventPipeline(config, KapotteDatabase(), bus, state)  # type: ignore[arg-type]

    async with bus.subscribe() as queue:
        await pipeline.start()
        pipeline.submit(_event("BA", "alarm", "burglary", device_id="01"))
        pipeline.submit(_event("BA", "alarm", "burglary", device_id="03"))
        await pipeline._queue.join()
        await pipeline.stop()
        codes = []
        while not queue.empty():
            codes.append(queue.get_nowait().code)

    # Beide alarmen komen door, en precies één storingsmelding over de opslag
    # (die sluit achteraan aan, want beide alarmen stonden al in de rij).
    assert codes == ["BA", "BA", "DBFAIL"]
    assert "system:systeem" in state.troubles


async def test_status_overleeft_een_herstart(config: Config, db: Database) -> None:
    from ajaxcentral.bus import EventBus

    bus: EventBus[AlarmEvent] = EventBus()
    state = SystemState(config)
    pipeline = EventPipeline(config, db, bus, state)
    await pipeline.start()

    pipeline.submit(_event("CL", "info", "arming", partition_id="1"))
    pipeline.submit(_event("XT", "trouble", "battery", device_id="03"))
    pipeline.submit(_event("BA", "alarm", "burglary", device_id="01"))
    await pipeline._queue.join()
    await pipeline.stop()

    hersteld = SystemState(config)
    await hersteld.restore_from_db(db)
    assert hersteld.partitions["1"].armed
    assert len(hersteld.troubles) == 1
    assert hersteld.open_alarms == 1
    # Na een herstart weten we niet of de hub er nog is tot hij iets stuurt.
    assert not hersteld.hub_online
