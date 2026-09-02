"""Opmaak van het Matrix-bericht bij een event.

Het bericht is wat je leest nadat je telefoon je wakker heeft gebeld. Het moet
in één blik de drie dingen geven waar je dan om geeft: wat, waar, en hoe laat.
"""

from __future__ import annotations

import html
from typing import Any

from ...config import Config
from ...models import AlarmEvent
from ..text import SEVERITY_LABELS, event_link, fact_lines

_ICONS: dict[str, str] = {
    "alarm": "🚨",
    "trouble": "⚠️",
    "restore": "✅",
    "info": "ℹ️",
    "heartbeat": "💓",
    "unknown": "❓",
}

_COLOURS: dict[str, str] = {
    "alarm": "#c0392b",
    "trouble": "#d68910",
    "restore": "#1e8449",
    "info": "#2471a3",
    "heartbeat": "#7f8c8d",
    "unknown": "#7d3c98",
}


def build_message(alarm: AlarmEvent, config: Config) -> dict[str, Any]:
    """Bouw de inhoud van een m.room.message-event."""
    icon = _ICONS.get(alarm.severity, "•")
    label = SEVERITY_LABELS.get(alarm.severity, alarm.severity)
    rows = fact_lines(alarm, config)
    link = event_link(alarm, config)

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
