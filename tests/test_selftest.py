"""De zelftest bewaakt of het belpad nog werkt."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ajaxcentral.config import Config
from ajaxcentral.db import Database
from ajaxcentral.selftest import SelfTest


class FakeRinger:
    """Een meldkanaal dat wel of niet lukt, zonder echt iets te versturen."""

    name = "fake"

    def __init__(self, *, succeeds: bool = True) -> None:
        self.succeeds = succeeds
        self.reasons: list[str] = []

    async def test_ring(self, reason: str) -> bool:
        self.reasons.append(reason)
        return self.succeeds


@pytest.mark.parametrize(
    ("moment", "verwacht"),
    [
        # Dinsdag 12:00 lokaal, test staat op 14:00 -> vandaag.
        ("2026-08-25T10:00:00", "2026-08-25 14:00"),
        # Dinsdag 14:01 lokaal -> pas volgende week.
        ("2026-08-25T12:01:00", "2026-09-01 14:00"),
        # Woensdag -> eerstvolgende dinsdag.
        ("2026-08-26T10:00:00", "2026-09-01 14:00"),
        # Wintertijd: één uur verschil met UTC in plaats van twee.
        ("2026-01-13T12:00:00", "2026-01-13 14:00"),
    ],
)
def test_planning_houdt_rekening_met_zomertijd(
    config: Config, db: Database, moment: str, verwacht: str
) -> None:
    selftest = SelfTest(config, db, None)
    now = datetime.fromisoformat(moment).replace(tzinfo=UTC)
    assert selftest.next_run_after(now).strftime("%Y-%m-%d %H:%M") == verwacht


async def test_status_zonder_uitgevoerde_test(config: Config, db: Database) -> None:
    status = await SelfTest(config, db, None).status()
    assert status["state"] == "nooit uitgevoerd"
    assert status["warning"] is True


def _selftest(config: Config, db: Database, *, succeeds: bool = True) -> SelfTest:
    return SelfTest(config, db, FakeRinger(succeeds=succeeds))


async def test_geslaagde_test_wacht_op_bevestiging(config: Config, db: Database) -> None:
    selftest = _selftest(config, db)
    run = await selftest.run_once(kind="manual")
    assert run.ring_status == "sent"

    status = await selftest.status()
    assert status["warning"] is False
    assert "wacht op bevestiging" in str(status["state"])


async def test_bevestigde_test_geeft_groen_licht(config: Config, db: Database) -> None:
    selftest = _selftest(config, db)
    await selftest.run_once(kind="manual")
    await selftest.acknowledge_latest("tom")

    status = await selftest.status()
    assert status["warning"] is False
    assert "werkt" in str(status["state"])


async def test_niet_bevestigde_test_slaat_alarm(config: Config, db: Database) -> None:
    """Zo ontdek je een kapot belpad op een dinsdagmiddag."""
    selftest = _selftest(config, db)
    run = await selftest.run_once(kind="scheduled")

    async with db.session() as session:
        from ajaxcentral.models import SelftestRun

        stored = await session.get(SelftestRun, run.id)
        assert stored is not None
        deadline = config.selftest.ack_deadline_minutes
        stored.started_at = datetime.now(UTC) - timedelta(minutes=deadline + 5)
        await session.commit()

    status = await selftest.status()
    assert status["warning"] is True
    assert "niet bevestigd" in str(status["state"])


async def test_mislukte_oproep_is_meteen_een_waarschuwing(config: Config, db: Database) -> None:
    selftest = _selftest(config, db, succeeds=False)
    run = await selftest.run_once(kind="manual")
    assert run.ring_status == "failed"

    status = await selftest.status()
    assert status["warning"] is True
