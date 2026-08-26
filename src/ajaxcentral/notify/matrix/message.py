"""Opmaak van het Matrix-bericht bij een event.

Het bericht is wat je leest nadat je telefoon je wakker heeft gebeld. Het moet
in één blik de drie dingen geven waar je dan om geeft: wat, waar, en hoe laat.
"""

from __future__ import annotations

import html
from typing import Any

from ...config import Config
from ...models import AlarmEvent

_ICONS: dict[str, str] = {
    "alarm": "🚨",
    "trouble": "⚠️",
    "restore": "✅",
    "info": "ℹ️",
    "heartbeat": "💓",
    "unknown": "❓",
}

_SEVERITY_LABELS: dict[str, str] = {
    "alarm": "ALARM",
    "trouble": "Storing",
    "restore": "Herstel",
    "info": "Info",
    "heartbeat": "Hartslag",
    "unknown": "Onbekend",
}

_COLOURS: dict[str, str] = {
    "alarm": "#c0392b",
    "trouble": "#d68910",
    "restore": "#1e8449",
    "info": "#2471a3",
    "heartbeat": "#7f8c8d",
    "unknown": "#7d3c98",
}


def _lines(alarm: AlarmEvent, config: Config) -> list[tuple[str, str]]:
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
    return rows


def build_message(alarm: AlarmEvent, config: Config) -> dict[str, Any]:
    """Bouw de inhoud van een m.room.message-event."""
    icon = _ICONS.get(alarm.severity, "•")
    label = _SEVERITY_LABELS.get(alarm.severity, alarm.severity)
    rows = _lines(alarm, config)
    link = f"{config.web.base_url}/#event-{alarm.db_id}" if alarm.db_id else config.web.base_url

    plain = [f"{icon} {label}: {alarm.summary()}"]
    plain += [f"{name}: {value}" for name, value in rows]
    if alarm.is_alarm:
        plain.append(f"Bevestigen: {link}")

    colour = _COLOURS.get(alarm.severity, "#2c3e50")
    rows_html = "".join(
        f"<li><b>{html.escape(name)}:</b> {html.escape(str(value))}</li>" for name, value in rows
    )
    formatted = (
        f'<p><b><font color="{colour}">{icon} {html.escape(label)}</font></b> — '
        f"{html.escape(alarm.summary())}</p><ul>{rows_html}</ul>"
    )
    if alarm.is_alarm:
        formatted += f'<p><a href="{html.escape(link)}">Bevestig dit alarm in het dashboard</a></p>'

    content: dict[str, Any] = {
        "msgtype": "m.text",
        "body": "\n".join(plain),
        "format": "org.matrix.custom.html",
        "formatted_body": formatted,
    }

    # Een alarm noemt je bij naam, zodat het ook doorkomt in een room die op
    # "alleen vermeldingen" staat.
    if alarm.is_alarm and config.matrix.target_user_id:
        content["m.mentions"] = {"user_ids": [config.matrix.target_user_id]}

    return content
