"""De belketen: payloads, escalatie en het stoppen daarvan."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ajaxcentral.config import Config
from ajaxcentral.db import Database
from ajaxcentral.models import AlarmEvent
from ajaxcentral.notify.matrix.client import MatrixClient, MatrixError
from ajaxcentral.notify.matrix.message import build_message
from ajaxcentral.notify.matrix.notifier import MatrixNotifier
from ajaxcentral.notify.matrix.ring import VARIANTS, RingSender
from ajaxcentral.notify.rules import NotificationRules


class FakeHomeserver:
    """Vangt alle Matrix-verzoeken op en kan gericht falen."""

    def __init__(self, fail_status: int | None = None) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_status = fail_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body))
        if request.url.path.endswith("/whoami"):
            return httpx.Response(200, json={"user_id": "@bot:test"})
        if self.fail_status:
            return httpx.Response(self.fail_status, json={"errcode": "M_FORBIDDEN"})
        return httpx.Response(200, json={"event_id": "$id"})

    def client(self, homeserver: str = "https://matrix.test") -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=homeserver, transport=httpx.MockTransport(self.handler))

    def sent_types(self) -> list[str]:
        out = []
        for _, path, _ in self.requests:
            if "/send/" in path:
                out.append(path.rsplit("/send/", 1)[-1].split("/")[0])
            elif "/state/" in path:
                out.append("state:" + path.rsplit("/state/", 1)[-1].split("/")[0])
        return out


def _alarm(**kwargs: Any) -> AlarmEvent:
    defaults: dict[str, Any] = {
        "code": "BA",
        "category": "burglary",
        "severity": "alarm",
        "title": "Inbraakalarm",
        "description": "",
        "device_id": "01",
        "device_name": "Voordeur",
        "partition_name": "Begane grond",
    }
    defaults.update(kwargs)
    return AlarmEvent(**defaults)


# ── Payloads ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", list(VARIANTS))
def test_elke_variant_richt_zich_op_de_juiste_persoon(config: Config, name: str) -> None:
    content = VARIANTS[name].build(config, "call123")
    assert content["m.mentions"]["user_ids"] == ["@tom:test"]
    assert content["call_id"] == "call123"


def test_legacy_variant_gebruikt_notify_type(config: Config) -> None:
    content = VARIANTS["call-notify-legacy"].build(config, "x")
    assert content["notify_type"] == "ring"
    assert content["application"] == "m.call"


def test_nieuwe_variant_stuurt_beide_veldnamen(config: Config) -> None:
    """De MSC hernoemde velden onderweg; een client negeert wat hij niet kent,
    maar een ontbrekend veld kost het rinkelen."""
    content = VARIANTS["rtc-notification"].build(config, "x")
    assert content["notification_type"] == "ring"
    assert content["notify_type"] == "ring"
    assert content["intent"] == content["m.call.intent"] == "voice"


async def test_ring_stuurt_alle_gekozen_varianten(config: Config) -> None:
    server = FakeHomeserver()
    client = MatrixClient("https://matrix.test", "token", "@bot:test")
    await client.start()
    client._client = server.client()

    result = await RingSender(client, config).ring("test")
    assert result.ok
    assert set(result.sent) == {"rtc-notification", "call-notify-legacy"}
    assert "state:m.rtc.member" in server.sent_types()
    await client.stop()


async def test_ring_meldt_falen(config: Config) -> None:
    server = FakeHomeserver(fail_status=403)
    client = MatrixClient("https://matrix.test", "token", "@bot:test")
    await client.start()
    client._client = server.client()

    result = await RingSender(client, config).ring("test")
    assert not result.ok
    assert len(result.failed) == 2
    await client.stop()


# ── Berichten ────────────────────────────────────────────────────────────────


def test_alarmbericht_noemt_je_bij_naam(config: Config) -> None:
    content = build_message(_alarm(), config)
    assert content["m.mentions"]["user_ids"] == ["@tom:test"]
    assert "Inbraakalarm" in content["body"]
    assert "Voordeur" in content["body"]


def test_gewoon_bericht_noemt_je_niet(config: Config) -> None:
    content = build_message(_alarm(severity="info", category="arming"), config)
    assert "m.mentions" not in content


def test_bericht_bevat_lokale_tijd(config: Config) -> None:
    from datetime import UTC, datetime

    alarm = _alarm(event_at=datetime(2026, 8, 26, 12, 0, tzinfo=UTC))
    # Europe/Amsterdam is in augustus twee uur voor op UTC.
    assert "14:00:00" in build_message(alarm, config)["body"]


# ── Regels ───────────────────────────────────────────────────────────────────


def test_alarm_belt_storing_niet(config: Config) -> None:
    rules = NotificationRules(config)
    assert rules.should_ring(_alarm())
    assert not rules.should_ring(_alarm(severity="trouble", category="fire"))
    assert not rules.should_ring(_alarm(category="access"))


def test_co_en_hitte_bellen_net_als_brand(config: Config) -> None:
    """Eén FireProtect Plus meldt rook, CO en hitte met drie codes."""
    rules = NotificationRules(config)
    for category in ("fire", "gas", "heat"):
        assert rules.should_ring(_alarm(category=category))


def test_stille_uren_smoren_nooit_een_alarm(config: Config) -> None:
    from datetime import datetime, time

    data = config.model_dump()
    data["notifications"]["quiet_hours"] = {
        "enabled": True,
        "start": time(0, 0),
        "end": time(23, 59),
        "allow_severities": [],
    }
    quiet = Config.model_validate(data)
    rules = NotificationRules(quiet)
    middle_of_night = datetime(2026, 8, 26, 3, 0)

    assert rules.should_notify(_alarm(), now=middle_of_night)
    assert not rules.should_notify(
        _alarm(severity="trouble", category="battery"), now=middle_of_night
    )


def test_herhaling_wordt_samengevoegd(config: Config) -> None:
    from datetime import datetime, timedelta

    rules = NotificationRules(config)
    now = datetime(2026, 8, 26, 12, 0)
    trouble = _alarm(severity="trouble", category="battery", device_id="03")

    assert rules.should_notify(trouble, now=now)
    assert not rules.should_notify(trouble, now=now + timedelta(seconds=5))
    assert rules.should_notify(trouble, now=now + timedelta(seconds=120))


def test_twee_melders_zijn_twee_meldingen(config: Config) -> None:
    from datetime import datetime

    rules = NotificationRules(config)
    now = datetime(2026, 8, 26, 12, 0)
    assert rules.should_notify(_alarm(severity="trouble", device_id="03"), now=now)
    assert rules.should_notify(_alarm(severity="trouble", device_id="04"), now=now)


# ── Escalatie ────────────────────────────────────────────────────────────────


async def _notifier(config: Config, db: Database, server: FakeHomeserver) -> MatrixNotifier:
    notifier = MatrixNotifier(config, db)
    await notifier.client.start()
    notifier.client._client = server.client()
    return notifier


async def test_escalatie_belt_tot_het_maximum(config: Config, db: Database) -> None:
    server = FakeHomeserver()
    notifier = await _notifier(config, db, server)
    alarm = _alarm()
    await db.store_event(alarm)

    await notifier.escalation.start_for(alarm)
    for task in list(notifier.escalation._tasks.values()):
        await task

    row = await db.get_event(alarm.db_id or 0)
    assert row is not None
    assert len(row.calls) == config.matrix.ring.max_attempts
    assert all(call.status == "sent" for call in row.calls)
    await notifier.stop()


async def test_bevestigen_stopt_het_bellen(config: Config, db: Database) -> None:
    import asyncio

    server = FakeHomeserver()
    notifier = await _notifier(config, db, server)
    alarm = _alarm()
    await db.store_event(alarm)

    await notifier.escalation.start_for(alarm)
    await asyncio.sleep(0.02)
    await db.acknowledge(alarm.db_id or 0, "tom")

    for task in list(notifier.escalation._tasks.values()):
        await task

    row = await db.get_event(alarm.db_id or 0)
    assert row is not None
    assert len(row.calls) < config.matrix.ring.max_attempts
    await notifier.stop()


async def test_open_alarm_wordt_hervat_na_herstart(config: Config, db: Database) -> None:
    """Een herstart mag een lopend alarm niet stilzetten."""
    server = FakeHomeserver()
    notifier = await _notifier(config, db, server)
    await db.store_event(_alarm())

    resumed = await notifier.escalation.resume_open_alarms()
    assert resumed == 1

    for task in list(notifier.escalation._tasks.values()):
        await task
    assert any("notif" in path or "notify" in path for _, path, _ in server.requests)
    await notifier.stop()


async def test_bevestigd_alarm_wordt_niet_hervat(config: Config, db: Database) -> None:
    server = FakeHomeserver()
    notifier = await _notifier(config, db, server)
    alarm = _alarm()
    await db.store_event(alarm)
    await db.acknowledge(alarm.db_id or 0, "tom")

    assert await notifier.escalation.resume_open_alarms() == 0
    await notifier.stop()


# ── Client ───────────────────────────────────────────────────────────────────


async def test_zelfde_event_levert_geen_dubbel_bericht(config: Config, db: Database) -> None:
    """Een herhaling na een netwerkfout mag niet twee keer aankomen."""
    server = FakeHomeserver()
    notifier = await _notifier(config, db, server)
    alarm = _alarm()
    await db.store_event(alarm)

    await notifier.send_event(alarm)
    await notifier.send_event(alarm)

    txn_ids = [path.rsplit("/", 1)[-1] for _, path, _ in server.requests if "/send/" in path]
    assert len(set(txn_ids)) == 1
    await notifier.stop()


async def test_client_probeert_niet_opnieuw_bij_403(config: Config) -> None:
    """Bij een verkeerd token verandert herhalen niets, en er loopt een alarm."""
    server = FakeHomeserver(fail_status=403)
    client = MatrixClient("https://matrix.test", "token", "@bot:test")
    await client.start()
    client._client = server.client()

    with pytest.raises(MatrixError):
        await client.send_event("!room:test", "m.room.message", {}, "txn")
    assert len(server.requests) == 1
    await client.stop()
