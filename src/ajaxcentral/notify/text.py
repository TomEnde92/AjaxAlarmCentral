"""Kanaalonafhankelijke tekst voor meldingen.

Elk kanaal vertelt hetzelfde: wat, waar, en hoe laat. De feitenregels staan
hier één keer, zodat een tweede kanaal niet ongemerkt iets anders gaat melden.
"""

from __future__ import annotations

from ..config import Config
from ..models import AlarmEvent

SEVERITY_LABELS: dict[str, str] = {
    "alarm": "ALARM",
    "trouble": "Storing",
    "restore": "Herstel",
    "info": "Info",
    "heartbeat": "Hartslag",
    "unknown": "Onbekend",
}


def fact_lines(alarm: AlarmEvent, config: Config) -> list[tuple[str, str]]:
    """De feitenregels onder de kop, in volgorde van belang."""
    local = config.to_local(alarm.event_at)
    rows: list[tuple[str, str]] = [("Tijd", local.strftime("%d-%m-%Y %H:%M:%S"))]

    if alarm.device_name != "systeem":
        rows.append(("Apparaat", alarm.device_name))
    if alarm.user_name:
        rows.append(("Gebruiker", alarm.user_name))
    if alarm.partition_name != "systeem":
        rows.append(("Groep", alarm.partition_name))
    rows.append(("Code", alarm.code))
    if alarm.message and alarm.message != alarm.device_id:
        rows.append(("Melding", alarm.message))
    if alarm.source == "internal":
        rows.append(("Bron", "de alarmcentrale zelf, niet de hub"))
    elif alarm.source == "test":
        rows.append(("Bron", "TESTALARM vanuit het dashboard, niet de hub"))
    return rows


def event_link(alarm: AlarmEvent, config: Config) -> str:
    """Link naar het event in het dashboard, of naar het dashboard zelf."""
    if alarm.db_id:
        return f"{config.web.base_url}/#event-{alarm.db_id}"
    return config.web.base_url


def plain_text(alarm: AlarmEvent, config: Config) -> tuple[str, str]:
    """Titel en platte tekst, voor kanalen zonder opmaak."""
    label = SEVERITY_LABELS.get(alarm.severity, alarm.severity)
    title = f"{label}: {alarm.summary()}"
    body = "\n".join(f"{name}: {value}" for name, value in fact_lines(alarm, config))
    return title, body
