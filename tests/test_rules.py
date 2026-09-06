"""Welke events een melding waard zijn: drempel, stille uren en herhaling."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from ajaxcentral.config import Config
from ajaxcentral.models import AlarmEvent
from ajaxcentral.notify.rules import NotificationRules


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


def test_stille_uren_smoren_nooit_een_alarm(config: Config) -> None:
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


def test_drempel_houdt_lichte_events_tegen(config: Config) -> None:
    """min_severity staat op "trouble": een inschakelmelding hoeft niet te piepen."""
    rules = NotificationRules(config)
    now = datetime(2026, 8, 26, 12, 0)
    assert not rules.should_notify(_alarm(severity="info", category="arming"), now=now)
    assert rules.should_notify(_alarm(severity="trouble", category="battery"), now=now)


def test_herhaling_wordt_samengevoegd(config: Config) -> None:
    rules = NotificationRules(config)
    now = datetime(2026, 8, 26, 12, 0)
    trouble = _alarm(severity="trouble", category="battery", device_id="03")

    assert rules.should_notify(trouble, now=now)
    assert not rules.should_notify(trouble, now=now + timedelta(seconds=5))
    assert rules.should_notify(trouble, now=now + timedelta(seconds=120))


def test_twee_melders_zijn_twee_meldingen(config: Config) -> None:
    rules = NotificationRules(config)
    now = datetime(2026, 8, 26, 12, 0)
    assert rules.should_notify(_alarm(severity="trouble", device_id="03"), now=now)
    assert rules.should_notify(_alarm(severity="trouble", device_id="04"), now=now)
