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


async def test_watchdog_meldt_uitval_en_herstel(config: Config, db: Database) -> None:
    from ajaxcentral.bus import EventBus

    bus: EventBus[AlarmEvent] = EventBus()
    state = SystemState(config)
    pipeline = EventPipeline(config, db, bus, state)
    watchdog = Watchdog(config, state, pipeline)

    state.started_at = utcnow() - timedelta(seconds=300)

    watchdog.check()
    watchdog.check()  # tweede ronde mag geen tweede alarm opleveren
    assert pipeline._queue.qsize() == 1
    first = pipeline._queue.get_nowait()
    assert first.code == "HUBOFF"
    assert first.severity == "alarm"

    state.note_contact()
    watchdog.check()
    watchdog.check()
    assert pipeline._queue.qsize() == 1
    assert pipeline._queue.get_nowait().code == "HUBON"


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
