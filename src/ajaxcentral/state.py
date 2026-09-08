"""Afgeleide systeemstatus: wat is er nú aan de hand?

De eventstroom vertelt wat er gebeurde; dit vertelt wat er geldt. Het dashboard
en de MQTT-statustopics lezen hieruit.

De status wordt bij het opstarten opnieuw opgebouwd uit de opgeslagen events,
zodat een herstart of stroomstoring geen geheugenverlies veroorzaakt: een
alarmcentrale die na een reboot denkt dat alles in orde is, is gevaarlijk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .ajax_codes import AREA_ARM_CODES, ARM_CODES, DISARM_CODES
from .config import Config
from .models import AlarmEvent, as_utc, iso, utcnow

_LOGGER = logging.getLogger(__name__)

#: Categorieën waarvan een "trouble" blijft staan tot er een herstel komt.
_STICKY_TROUBLE_CATEGORIES = {
    "power",
    "battery",
    "rf",
    "communication",
    "supervision",
    "burglary",
    "fire",
    "gas",
    "heat",
    "water",
    "tamper",
    "system",
}


@dataclass(slots=True)
class PartitionState:
    partition_id: str
    name: str
    armed: bool = False
    changed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "partition_id": self.partition_id,
            "name": self.name,
            "armed": self.armed,
            "changed_at": iso(self.changed_at),
        }


@dataclass(slots=True)
class Trouble:
    key: str
    category: str
    title: str
    device_name: str
    since: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "category": self.category,
            "title": self.title,
            "device_name": self.device_name,
            "since": iso(self.since),
        }


class SystemState:
    def __init__(self, config: Config) -> None:
        self._config = config
        self.last_contact: datetime | None = None
        self.hub_online: bool = False
        self.partitions: dict[str, PartitionState] = {}
        self.troubles: dict[str, Trouble] = {}
        self.open_alarms: int = 0
        self.last_alarm: AlarmEvent | None = None
        self.started_at: datetime = utcnow()

        for partition_id, name in config.partitions.items():
            self.partitions[partition_id] = PartitionState(partition_id, name)

    # ── Bijwerken ────────────────────────────────────────────────────────────

    def note_contact(self, when: datetime | None = None) -> bool:
        """Registreer dat de hub van zich liet horen.

        Geeft True terug als dit een overgang van offline naar online is, zodat
        de aanroeper daar een herstelmelding van kan maken.
        """
        self.last_contact = when or utcnow()
        was_offline = not self.hub_online
        self.hub_online = True
        return was_offline

    def mark_offline(self) -> bool:
        """Markeer de hub als onbereikbaar. True bij een echte overgang."""
        was_online = self.hub_online
        self.hub_online = False
        return was_online

    def apply(self, alarm: AlarmEvent) -> None:
        """Werk de status bij op basis van één event."""
        if alarm.source == "hub":
            self.note_contact(alarm.received_at)

        if alarm.code in ARM_CODES or alarm.code in DISARM_CODES:
            self._apply_arming(alarm, armed=alarm.code in ARM_CODES)

        if alarm.severity == "trouble" and alarm.category in _STICKY_TROUBLE_CATEGORIES:
            key = self._trouble_key(alarm)
            self.troubles[key] = Trouble(
                key=key,
                category=alarm.category,
                title=alarm.title,
                device_name=alarm.device_name,
                since=alarm.received_at,
            )
        elif alarm.severity == "restore":
            # Een herstel ruimt de storing én het alarm van dezelfde melder op.
            self.troubles.pop(self._trouble_key(alarm), None)

        if alarm.severity == "alarm":
            self.last_alarm = alarm

    def _apply_arming(self, alarm: AlarmEvent, *, armed: bool) -> None:
        partition_id = alarm.partition_id or "1"
        if alarm.code in AREA_ARM_CODES:
            targets = [partition_id]
        else:
            # De hele installatie: alle groepen die we kennen, plus de groep
            # uit het bericht als die nog niet in de configuratie staat.
            targets = list(self.partitions)
            if partition_id not in self.partitions:
                targets.append(partition_id)

        for target in targets:
            partition = self.partitions.get(target)
            if partition is None:
                partition = PartitionState(target, self._config.partition_name(target))
                self.partitions[target] = partition
            partition.armed = armed
            partition.changed_at = alarm.received_at

    @staticmethod
    def _trouble_key(alarm: AlarmEvent) -> str:
        return f"{alarm.category}:{alarm.device_id or 'systeem'}"

    # ── Uitlezen ─────────────────────────────────────────────────────────────

    @property
    def seconds_since_contact(self) -> float | None:
        if self.last_contact is None:
            return None
        return (utcnow() - as_utc(self.last_contact)).total_seconds()

    def is_stale(self, threshold_seconds: float) -> bool:
        """Heeft de hub te lang gezwegen?

        Voordat er ooit contact is geweest rekenen we vanaf de starttijd, zodat
        """Zet de betrokken groepen aan of uit.

        Welke groepen dat zijn, staat in de code en niet in het groepsveld. Een
        CL ("System armed, normal") of OP ("Account was disarmed") gaat over de
        hele installatie: het nummer erin is de gebruiker, en het groepsveld
        zet de hub daarbij gewoon op 1. Alleen de gebiedscodes uit
        AREA_ARM_CODES — de deel- en nachtinschakeling voorop — betreffen één
        groep.

        Zonder dat onderscheid blijft de verdieping op het dashboard
        "Uitgeschakeld" heten terwijl het hele huis is ingeschakeld, en dat is
        precies het soort halve waarheid waar je 's nachts niet op wilt
        vertrouwen.
        """
        een centrale die naast een uitgeschakelde hub opstart óók alarm slaat in
        plaats van eeuwig te wachten op een eerste bericht.
        """
        reference = self.last_contact or self.started_at
        return (utcnow() - as_utc(reference)).total_seconds() > threshold_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "hub_online": self.hub_online,
            "last_contact": iso(self.last_contact),
            "seconds_since_contact": self.seconds_since_contact,
            "partitions": [p.to_dict() for p in self.partitions.values()],
            "any_armed": any(p.armed for p in self.partitions.values()),
            "troubles": [t.to_dict() for t in self.troubles.values()],
            "open_alarms": self.open_alarms,
            "last_alarm": self.last_alarm.to_dict() if self.last_alarm else None,
            "started_at": iso(self.started_at),
        }

    # ── Herstel na een herstart ──────────────────────────────────────────────

    async def restore_from_db(self, db: Any, lookback_days: int = 7) -> None:
        """Bouw de status opnieuw op uit de opgeslagen events.

        We spelen de recente geschiedenis in chronologische volgorde af. Dat is
        eenvoudiger en betrouwbaarder dan de status apart bijhouden, want het
        logboek is sowieso de bron van waarheid.
        """
        since = utcnow() - timedelta(days=lookback_days)
        rows = await db.list_events(limit=5000, since=since)
        for row in reversed(list(rows)):
            self.apply(
                AlarmEvent(
                    code=row.code,
                    category=row.category,
                    severity=row.severity,
                    title=row.title,
                    description=row.description,
                    source=row.source,
                    device_id=row.device_id,
                    device_name=row.device_name,
                    partition_id=row.partition_id,
                    partition_name=row.partition_name,
                    received_at=as_utc(row.received_at),
                    event_at=as_utc(row.event_at),
                    uid=row.uid,
                    db_id=row.id,
                )
            )
        self.open_alarms = len(await db.unacknowledged_alarms())
        # De hub geldt pas weer als online zodra hij zelf iets stuurt; na een
        # herstart weten we het simpelweg nog niet.
        self.hub_online = False
        _LOGGER.info(
            "Status hersteld uit %d events: %d groepen, %d storingen, %d open alarmen",
            len(rows),
            len(self.partitions),
            len(self.troubles),
            self.open_alarms,
        )
