"""De tekst van een melding: wat, waar, en hoe laat."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ajaxcentral.config import Config
from ajaxcentral.models import AlarmEvent
from ajaxcentral.notify.text import event_link, fact_lines, plain_text


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


def test_alarmbericht_noemt_wat_en_waar(config: Config) -> None:
    title, body = plain_text(_alarm(), config)
    assert title == "ALARM: Inbraakalarm — Voordeur (Begane grond)"
    assert "Apparaat: Voordeur" in body
    assert "Groep: Begane grond" in body
    assert "Code: BA" in body


def test_bericht_bevat_lokale_tijd(config: Config) -> None:
    """UTC in de database, Europe/Amsterdam op je telefoon."""
    alarm = _alarm(event_at=datetime(2026, 8, 26, 10, 30, 0, tzinfo=UTC))
    tijd = dict(fact_lines(alarm, config))["Tijd"]
    assert tijd == "26-08-2026 12:30:00"


def test_inschakelmelding_noemt_de_persoon(config: Config) -> None:
    alarm = _alarm(
        code="NL",
        category="arming",
        severity="info",
        title="Nachtinschakeling",
        device_id=None,
        device_name="systeem",
        user_name="Tom",
    )
    title, body = plain_text(alarm, config)
    assert title == "Info: Nachtinschakeling door Tom"
    assert "Gebruiker: Tom" in body


def test_link_wijst_naar_het_event_als_dat_kan(config: Config) -> None:
    assert event_link(_alarm(), config) == config.web.base_url
    assert event_link(_alarm(db_id=7), config) == f"{config.web.base_url}/#event-7"
