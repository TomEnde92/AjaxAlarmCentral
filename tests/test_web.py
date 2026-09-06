"""Het dashboard: inloggen, bevestigen en de API."""

from __future__ import annotations

import httpx
import pytest

from ajaxcentral.bus import EventBus
from ajaxcentral.config import Config
from ajaxcentral.db import Database
from ajaxcentral.models import AlarmEvent
from ajaxcentral.state import SystemState
from ajaxcentral.web.app import WebContext, create_app
from ajaxcentral.web.auth import hash_password, verify_password

PASSWORD = "geheim123"


@pytest.fixture
def web_config(config: Config) -> Config:
    data = config.model_dump()
    data["web"]["password_hash"] = hash_password(PASSWORD)
    data["web"]["secret"] = "test-secret"
    return Config.model_validate(data)


@pytest.fixture
def context(web_config: Config, db: Database) -> WebContext:
    return WebContext(
        config=web_config,
        db=db,
        bus=EventBus(),
        state=SystemState(web_config),
    )


async def _client(context: WebContext) -> httpx.AsyncClient:
    app = create_app(context)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# ── Wachtwoorden ─────────────────────────────────────────────────────────────


def test_wachtwoord_hashen_en_verifieren() -> None:
    encoded = hash_password("wachtwoord")
    assert verify_password("wachtwoord", encoded)
    assert not verify_password("Wachtwoord", encoded)
    assert not verify_password("", encoded)


def test_hash_is_elke_keer_anders() -> None:
    """Zonder eigen salt per hash zijn twee gelijke wachtwoorden herkenbaar."""
    assert hash_password("x") != hash_password("x")


def test_kapotte_hash_geeft_geen_toegang() -> None:
    for rommel in ("", "nonsens", "pbkdf2_sha256$abc", "a$b$c$d"):
        assert not verify_password("x", rommel)


# ── Toegang ──────────────────────────────────────────────────────────────────


async def test_api_is_dicht_zonder_inloggen(context: WebContext) -> None:
    async with await _client(context) as client:
        for path in ("/api/status", "/api/events", "/api/alarms", "/api/diagnostics"):
            assert (await client.get(path)).status_code == 401


async def test_inloggen(context: WebContext) -> None:
    async with await _client(context) as client:
        bad = await client.post("/api/login", json={"username": "admin", "password": "fout"})
        assert bad.status_code == 401

        good = await client.post("/api/login", json={"username": "admin", "password": PASSWORD})
        assert good.status_code == 200
        assert (await client.get("/api/status")).status_code == 200


async def test_onbekende_gebruiker_geeft_dezelfde_fout(context: WebContext) -> None:
    """Verklap niet of een gebruikersnaam bestaat."""
    async with await _client(context) as client:
        unknown = await client.post(
            "/api/login", json={"username": "nietbestaand", "password": PASSWORD}
        )
        wrong = await client.post("/api/login", json={"username": "admin", "password": "fout"})
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["detail"] == wrong.json()["detail"]


async def test_uitloggen(context: WebContext) -> None:
    async with await _client(context) as client:
        await client.post("/api/login", json={"username": "admin", "password": PASSWORD})
        await client.post("/api/logout")
        assert (await client.get("/api/status")).status_code == 401


# ── Events ───────────────────────────────────────────────────────────────────


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


async def test_logboek_verbergt_hartslagen(context: WebContext) -> None:
    """Bij een ping van een minuut zijn dat 1400 regels per dag."""
    await context.db.store_event(_alarm())
    await context.db.store_event(
        _alarm(code="RP", category="test", severity="heartbeat", title="Periodieke test")
    )

    async with await _client(context) as client:
        await client.post("/api/login", json={"username": "admin", "password": PASSWORD})

        default = (await client.get("/api/events")).json()["events"]
        assert [event["code"] for event in default] == ["BA"]

        with_heartbeat = (
            await client.get("/api/events", params={"include_heartbeat": "true"})
        ).json()["events"]
        assert len(with_heartbeat) == 2


async def test_datumfilter_en_csv_export(context: WebContext) -> None:
    """Een aangifte vraagt om een lijst met lokale tijden, niet om een JSON-dump."""
    from datetime import UTC, datetime

    old = _alarm(code="BA", received_at=datetime(2026, 1, 10, 12, 0, tzinfo=UTC))
    new = _alarm(
        code="FA",
        category="fire",
        title="Brandalarm",
        received_at=datetime(2026, 3, 5, 12, 0, tzinfo=UTC),
    )
    await context.db.store_event(old)
    await context.db.store_event(new)

    async with await _client(context) as client:
        await client.post("/api/login", json={"username": "admin", "password": PASSWORD})

        filtered = (
            await client.get("/api/events", params={"since": "2026-02-01T00:00:00+00:00"})
        ).json()["events"]
        assert [event["code"] for event in filtered] == ["FA"]

        bounded = (
            await client.get("/api/events", params={"since": "2026-01-01", "until": "2026-02-01"})
        ).json()["events"]
        assert [event["code"] for event in bounded] == ["BA"]

        assert (await client.get("/api/events", params={"since": "gisteren"})).status_code == 400

        response = await client.get("/api/events.csv", params={"until": "2026-02-01"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "logboek-" in response.headers["content-disposition"]
        lines = response.text.lstrip("\ufeff").splitlines()
        assert lines[0].startswith("tijdstip;ontvangen;ernst;")
        assert len(lines) == 2
        # 12:00 UTC in januari is 13:00 in Amsterdam.
        assert lines[1].split(";")[1] == "2026-01-10 13:00:00"
        assert ";BA;Inbraakalarm;Voordeur;" in lines[1]


async def test_bevestigen_stopt_de_escalatie(context: WebContext) -> None:
    alarm = _alarm()
    await context.db.store_event(alarm)
    gestopt: list[int] = []
    context.on_acknowledge = gestopt.append

    async with await _client(context) as client:
        await client.post("/api/login", json={"username": "admin", "password": PASSWORD})
        assert len((await client.get("/api/alarms")).json()["alarms"]) == 1

        response = await client.post(f"/api/events/{alarm.db_id}/acknowledge")
        assert response.status_code == 200
        assert response.json()["event"]["acknowledged_by"] == "admin"
        assert gestopt == [alarm.db_id]
        assert (await client.get("/api/alarms")).json()["alarms"] == []


async def test_alles_bevestigen(context: WebContext) -> None:
    for _ in range(3):
        await context.db.store_event(_alarm())
    gestopt: list[int] = []
    context.on_acknowledge = gestopt.append

    async with await _client(context) as client:
        await client.post("/api/login", json={"username": "admin", "password": PASSWORD})
        response = await client.post("/api/alarms/acknowledge-all")
        assert response.json()["acknowledged"] == 3
        assert len(gestopt) == 3


async def test_onbekend_event_bevestigen(context: WebContext) -> None:
    async with await _client(context) as client:
        await client.post("/api/login", json={"username": "admin", "password": PASSWORD})
        assert (await client.post("/api/events/999/acknowledge")).status_code == 404


async def test_status_toont_mislukte_meldingen(context: WebContext) -> None:
    """Een centrale die stil faalt geeft schijnveiligheid."""
    alarm = _alarm()
    await context.db.store_event(alarm)
    await context.db.log_notification(alarm.db_id, "pushover", "failed", "geen verbinding")

    async with await _client(context) as client:
        await client.post("/api/login", json={"username": "admin", "password": PASSWORD})
        status = (await client.get("/api/status")).json()
        assert status["failed_notifications_24h"] == 1
        assert status["open_alarms"] == 1


async def test_zelftest_zonder_meldkanaal_geeft_nette_fout(context: WebContext) -> None:
    async with await _client(context) as client:
        await client.post("/api/login", json={"username": "admin", "password": PASSWORD})
        assert (await client.post("/api/selftest/ring")).status_code == 503


# ── Testalarm ────────────────────────────────────────────────────────────────


class _Ingeleverd(list[AlarmEvent]):
    """Vervangt pipeline.submit: onthoudt wat de webapp zou inleveren."""

    def __call__(self, alarm: AlarmEvent) -> None:
        self.append(alarm)


@pytest.fixture
def context_met_kanaal(context: WebContext) -> WebContext:
    """Een context alsof er een meldkanaal aan staat: zelftest plus pijplijn-invoer."""
    from ajaxcentral.selftest import SelfTest

    context.selftest = SelfTest(context.config, context.db, None)
    context.submit = _Ingeleverd()
    return context


async def test_testalarm_zonder_meldkanaal_geeft_nette_fout(context: WebContext) -> None:
    async with await _client(context) as client:
        await client.post("/api/login", json={"username": "admin", "password": PASSWORD})
        response = await client.post("/api/selftest/alarm", json={"kind": "fire"})
        assert response.status_code == 503


async def test_testalarm_gaat_de_pijplijn_in(context_met_kanaal: WebContext) -> None:
    """Het testalarm volgt de echte route; de webapp bouwt alleen het event."""
    context = context_met_kanaal
    async with await _client(context) as client:
        await client.post("/api/login", json={"username": "admin", "password": PASSWORD})
        response = await client.post(
            "/api/selftest/alarm", json={"kind": "burglary", "device_id": "01"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["event"]["summary"] == "Inbraakalarm (TEST) — Voordeur"

    submitted = context.submit
    assert len(submitted) == 1
    alarm = submitted[0]
    assert alarm.code == "BA"
    assert alarm.severity == "alarm"
    assert alarm.source == "test"
    assert alarm.message == "Testalarm gestart door admin"


async def test_testalarm_weigert_onzin(context_met_kanaal: WebContext) -> None:
    context = context_met_kanaal
    async with await _client(context) as client:
        await client.post("/api/login", json={"username": "admin", "password": PASSWORD})
        assert (
            await client.post("/api/selftest/alarm", json={"kind": "meteoriet"})
        ).status_code == 400
        assert (
            await client.post("/api/selftest/alarm", json={"kind": "fire", "device_id": "99"})
        ).status_code == 400
    assert context.submit == []


async def test_testalarm_weigert_verkeerd_soort_melder(context_met_kanaal: WebContext) -> None:
    """Een rookmelder die inbraak meldt bestaat niet; zo'n test bewijst niets."""
    context = context_met_kanaal
    async with await _client(context) as client:
        await client.post("/api/login", json={"username": "admin", "password": PASSWORD})
        wrong = await client.post(
            "/api/selftest/alarm", json={"kind": "burglary", "device_id": "04"}
        )
        assert wrong.status_code == 400
        assert "Rookmelder" in wrong.json()["detail"]
        # Zonder ingesteld soort mag het wel; de rookmelder bij brand ook.
        assert (
            await client.post("/api/selftest/alarm", json={"kind": "fire", "device_id": "03"})
        ).status_code == 200
        assert (
            await client.post("/api/selftest/alarm", json={"kind": "fire", "device_id": "04"})
        ).status_code == 200
    assert [alarm.device_id for alarm in context.submit] == ["03", "04"]


async def test_lijst_van_testalarmen_toont_alleen_tests(context_met_kanaal: WebContext) -> None:
    context = context_met_kanaal
    await context.db.store_event(_alarm())  # echt alarm, hoort er niet bij
    from ajaxcentral.normalize import simulated_alarm

    test = simulated_alarm("fire", context.config, device_id="04", by="admin")
    await context.db.store_event(test)
    await context.db.log_notification(test.db_id, "pushover", "sent", test.summary())

    async with await _client(context) as client:
        await client.post("/api/login", json={"username": "admin", "password": PASSWORD})
        data = (await client.get("/api/selftest/alarms")).json()
        assert [event["code"] for event in data["alarms"]] == ["FA"]
        assert data["alarms"][0]["source"] == "test"
        assert data["alarms"][0]["notifications"][0]["channel"] == "pushover"
        assert data["kinds"] == ["fire", "burglary"]
        assert {"id": "04", "name": "Rookmelder", "type": "fire"} in data["devices"]
        assert {"id": "03", "name": "Bewegingsmelder", "type": None} in data["devices"]
