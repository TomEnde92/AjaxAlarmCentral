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
    await context.db.log_notification(alarm.db_id, "matrix", "failed", "geen verbinding")

    async with await _client(context) as client:
        await client.post("/api/login", json={"username": "admin", "password": PASSWORD})
        status = (await client.get("/api/status")).json()
        assert status["failed_notifications_24h"] == 1
        assert status["open_alarms"] == 1


async def test_zelftest_zonder_matrix_geeft_nette_fout(context: WebContext) -> None:
    async with await _client(context) as client:
        await client.post("/api/login", json={"username": "admin", "password": PASSWORD})
        assert (await client.post("/api/selftest/ring")).status_code == 503
