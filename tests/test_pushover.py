"""Pushover: noodmelding, bevestiging over en weer, en de zelftest."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest

from ajaxcentral.config import Config
from ajaxcentral.db import Database
from ajaxcentral.models import AlarmEvent
from ajaxcentral.normalize import simulated_alarm
from ajaxcentral.notify.base import NotifyError
from ajaxcentral.notify.pushover import PushoverNotifier
from ajaxcentral.selftest import SelfTest


class FakePushover:
    """Net genoeg van api.pushover.net om de keten te testen."""

    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []
        self.cancelled: list[str] = []
        self.acknowledged = False
        self.expired = False
        self.reject = False
        self._receipts = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/1/users/validate.json":
            return httpx.Response(200, json={"status": 1, "devices": ["telefoon"]})
        if path == "/1/messages.json":
            if self.reject:
                return httpx.Response(
                    400, json={"status": 0, "errors": ["application token is invalid"]}
                )
            form = dict(httpx.QueryParams(request.content.decode()))
            self.messages.append(form)
            body: dict[str, object] = {"status": 1, "request": "r"}
            if form.get("priority") == "2":
                self._receipts += 1
                body["receipt"] = f"rcpt{self._receipts}"
            return httpx.Response(200, json=body)
        if path.startswith("/1/receipts/") and path.endswith("/cancel.json"):
            self.cancelled.append(path.split("/")[3])
            return httpx.Response(200, json={"status": 1})
        if path.startswith("/1/receipts/"):
            return httpx.Response(
                200,
                json={
                    "status": 1,
                    "acknowledged": 1 if self.acknowledged else 0,
                    "acknowledged_by_device": "telefoon",
                    "expired": 1 if self.expired else 0,
                },
            )
        return httpx.Response(404, json={"status": 0, "errors": ["onbekend pad"]})


@pytest.fixture
def po_config(config: Config) -> Config:
    data = config.model_dump()
    data["pushover"] = {
        "enabled": True,
        "user_key": "u" * 30,
        "token": "t" * 30,
        "poll_interval_seconds": 0.01,
    }
    return Config.model_validate(data)


async def _notifier(
    config: Config, db: Database, fake: FakePushover, on_ack: Callable[[int], None] | None = None
) -> PushoverNotifier:
    notifier = PushoverNotifier(config, db, on_acknowledged=on_ack)
    notifier._client = httpx.AsyncClient(
        base_url=config.pushover.api_url, transport=httpx.MockTransport(fake.handler)
    )
    await notifier.start()
    return notifier


async def _wait_until(condition: Callable[[], object], timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("conditie werd niet waar binnen de tijd")
        await asyncio.sleep(0.02)


def _alarm(**kwargs: object) -> AlarmEvent:
    base: dict[str, object] = {
        "code": "BA",
        "category": "burglary",
        "severity": "alarm",
        "title": "Inbraakalarm",
        "description": "",
        "device_id": "01",
        "device_name": "Voordeur",
    }
    base.update(kwargs)
    return AlarmEvent(**base)  # type: ignore[arg-type]


async def test_alarm_wordt_noodmelding_en_bevestiging_komt_terug(
    po_config: Config, db: Database
) -> None:
    fake = FakePushover()
    acknowledged: list[int] = []
    notifier = await _notifier(po_config, db, fake, acknowledged.append)
    alarm = _alarm()
    await db.store_event(alarm)

    await notifier.send_event(alarm)
    sent = fake.messages[-1]
    assert sent["priority"] == "2"
    assert sent["retry"] == "30"
    assert sent["expire"] == "3600"
    assert sent["sound"] == "siren"
    assert sent["title"] == "ALARM: Inbraakalarm — Voordeur"
    assert "Apparaat: Voordeur" in sent["message"]
    assert await db.get_state("pushover_receipts") == {str(alarm.db_id): "rcpt1"}

    # Nog niet bevestigd: niets verandert.
    await asyncio.sleep(0.05)
    row = await db.get_event(alarm.db_id)
    assert row is not None and row.acknowledged_at is None

    # Op de telefoon bevestigd: database bijgewerkt, andere kanalen gewaarschuwd.
    fake.acknowledged = True
    await _wait_until(lambda: acknowledged == [alarm.db_id])
    row = await db.get_event(alarm.db_id)
    assert row is not None
    assert row.acknowledged_by == "pushover (telefoon)"
    assert await db.get_state("pushover_receipts") == {}
    await notifier.stop()


async def test_storing_is_gewoon_bericht_zonder_ontvangstbewijs(
    po_config: Config, db: Database
) -> None:
    fake = FakePushover()
    notifier = await _notifier(po_config, db, fake)
    alarm = _alarm(code="XT", category="battery", severity="trouble", title="Batterij bijna leeg")
    await db.store_event(alarm)
    await notifier.send_event(alarm)
    assert fake.messages[-1]["priority"] == "1"
    assert "retry" not in fake.messages[-1]
    assert notifier._watchers == {}
    await notifier.stop()


async def test_bevestigen_in_dashboard_trekt_noodmelding_in(
    po_config: Config, db: Database
) -> None:
    fake = FakePushover()
    notifier = await _notifier(po_config, db, fake)
    alarm = _alarm()
    await db.store_event(alarm)
    await notifier.send_event(alarm)
    assert alarm.db_id is not None

    await db.acknowledge(alarm.db_id, "tom")
    notifier.cancel_for(alarm.db_id)  # wat het dashboard via on_acknowledge doet
    await _wait_until(lambda: fake.cancelled == ["rcpt1"])
    assert await db.get_state("pushover_receipts") == {}
    await notifier.stop()


async def test_verlopen_noodmelding_wordt_vastgelegd(po_config: Config, db: Database) -> None:
    fake = FakePushover()
    notifier = await _notifier(po_config, db, fake)
    alarm = _alarm()
    await db.store_event(alarm)
    await notifier.send_event(alarm)
    fake.expired = True
    await _wait_until(lambda: not notifier._watchers)
    row = await db.get_event(alarm.db_id)
    assert row is not None
    assert row.acknowledged_at is None
    assert any(n.status == "expired" for n in row.notifications)
    await notifier.stop()


async def test_geweigerde_melding_geeft_fout_en_wordt_gelogd(
    po_config: Config, db: Database
) -> None:
    fake = FakePushover()
    fake.reject = True
    notifier = await _notifier(po_config, db, fake)
    alarm = _alarm()
    await db.store_event(alarm)
    with pytest.raises(NotifyError, match="token is invalid"):
        await notifier.send_event(alarm)
    row = await db.get_event(alarm.db_id)
    assert row is not None
    assert [n.status for n in row.notifications] == ["failed"]
    await notifier.stop()


async def test_lopende_noodmelding_overleeft_herstart(po_config: Config, db: Database) -> None:
    fake = FakePushover()
    first = await _notifier(po_config, db, fake)
    alarm = _alarm()
    await db.store_event(alarm)
    await first.send_event(alarm)
    await first.stop()  # "herstart": de watcher is weg, het bewijs staat in de database

    acknowledged: list[int] = []
    second = await _notifier(po_config, db, fake, acknowledged.append)
    assert str(alarm.db_id) in second._watchers
    fake.acknowledged = True
    await _wait_until(lambda: acknowledged == [alarm.db_id])
    await second.stop()


async def test_zelftest_via_pushover_bevestigt_zichzelf(po_config: Config, db: Database) -> None:
    fake = FakePushover()
    notifier = await _notifier(po_config, db, fake)
    selftest = SelfTest(po_config, db, notifier)
    notifier.on_selftest_acknowledged = selftest.acknowledge_latest

    run = await selftest.run_once(kind="manual")
    assert run.ring_status == "sent"
    assert fake.messages[-1]["priority"] == "2"
    assert fake.messages[-1]["expire"] == "600"

    fake.acknowledged = True

    async def _bevestigd() -> bool:
        latest = await selftest.latest()
        return latest is not None and latest.acknowledged_at is not None

    for _ in range(100):
        if await _bevestigd():
            break
        await asyncio.sleep(0.02)
    status = await selftest.status()
    assert status["warning"] is False
    assert "belpad werkt" in str(status["state"])
    assert "pushover (telefoon)" in str(status["last"]["detail"])
    await notifier.stop()


async def test_testalarm_gaat_als_echte_noodmelding(po_config: Config, db: Database) -> None:
    """Een nagebootst brandalarm moet de sirene laten afgaan, niet een stil berichtje."""
    fake = FakePushover()
    notifier = await _notifier(po_config, db, fake)
    alarm = simulated_alarm("fire", po_config, device_id="04", by="tom")
    await db.store_event(alarm)
    try:
        await notifier.send_event(alarm)
    finally:
        await notifier.stop()

    assert len(fake.messages) == 1
    sent = fake.messages[0]
    assert sent["priority"] == "2"
    assert sent["title"] == "ALARM: Brandalarm (TEST) — Rookmelder"
    assert "TESTALARM" in sent["message"]
