"""Domeinmodel en databasetabellen."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """SQLite bewaart geen tijdzone; wat eruit komt is naïeve UTC.

    Zonder deze correctie rekent het dashboard met lokale tijd op een UTC-waarde
    en staan alle tijdstempels er uren naast.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def iso(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value is not None else None


def build_summary(title: str, device_name: str, partition_name: str, user_name: str | None) -> str:
    """Eén leesbare regel over een event.

    In- en uitschakelen gaat over een persoon, alarmen over een plek. Beide in
    hetzelfde "titel — nummer" gietvormpje persen levert onzin op als
    "Ingeschakeld — Voordeur" waar 01 de gebruiker is, of "Dealer ID — systeem"
    waar het nummer nergens naar verwijst.

    Staat hier los van beide klassen omdat zowel het domeinobject als de
    databaserij hem nodig heeft, en twee kopieën vroeg of laat uit elkaar lopen.
    """
    if user_name:
        return f"{title} door {user_name}"
    if device_name == "systeem" and partition_name == "systeem":
        return title
    where = device_name
    if partition_name not in ("systeem", where):
        where = f"{where} ({partition_name})" if where != "systeem" else partition_name
    return f"{title} — {where}"


# ── Domeinmodel ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class AlarmEvent:
    """Een genormaliseerd event, klaar voor opslag, dashboard en meldingen.

    Dit is bewust losgekoppeld van pysiaalarm's SIAEvent: de rest van de
    applicatie hoeft niets van het SIA-protocol te weten, en events die wij
    zelf verzinnen (watchdog, zelftest) zien er exact hetzelfde uit als events
    van de hub.
    """

    code: str
    category: str
    severity: str
    title: str
    description: str
    source: str = "hub"
    account: str | None = None
    device_id: str | None = None
    device_name: str = "systeem"
    partition_id: str | None = None
    partition_name: str = "systeem"
    user_id: str | None = None
    user_name: str | None = None
    zone: str | None = None
    message: str | None = None
    raw: str | None = None
    event_at: datetime = field(default_factory=utcnow)
    received_at: datetime = field(default_factory=utcnow)
    #: Stabiele sleutel voor idempotentie richting Matrix en voor deduplicatie.
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)
    #: Wordt gezet zodra het event is weggeschreven.
    db_id: int | None = None

    @property
    def is_alarm(self) -> bool:
        return self.severity == "alarm"

    @property
    def dedupe_key(self) -> str:
        return f"{self.code}|{self.device_id}|{self.partition_id}"

    def summary(self) -> str:
        """Eén regel, zoals die in Matrix en op het dashboard verschijnt."""
        return build_summary(self.title, self.device_name, self.partition_name, self.user_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.db_id,
            "uid": self.uid,
            "code": self.code,
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "description": self.description,
            "source": self.source,
            "account": self.account,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "partition_id": self.partition_id,
            "partition_name": self.partition_name,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "zone": self.zone,
            "message": self.message,
            "event_at": iso(self.event_at),
            "received_at": iso(self.received_at),
            "summary": self.summary(),
        }


# ── Databasetabellen ─────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    code: Mapped[str] = mapped_column(String(16), index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(16), default="hub")

    account: Mapped[str | None] = mapped_column(String(16), nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    device_name: Mapped[str] = mapped_column(String(128), default="systeem")
    partition_id: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    partition_name: Mapped[str] = mapped_column(String(128), default="systeem")
    user_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    user_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    zone: Mapped[str | None] = mapped_column(String(16), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Bevestigen stopt de escalatie. Bewust op het event en niet in het
    #: geheugen, zodat een herstart geen lopend alarm laat verdwijnen.
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    notifications: Mapped[list[NotificationLog]] = relationship(
        back_populates="event", cascade="all, delete-orphan", lazy="selectin"
    )
    calls: Mapped[list[CallAttempt]] = relationship(
        back_populates="event", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("ix_events_severity_received", "severity", "received_at"),)

    def to_dict(self, include_children: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "uid": self.uid,
            "received_at": iso(self.received_at),
            "event_at": iso(self.event_at),
            "code": self.code,
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "description": self.description,
            "source": self.source,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "partition_id": self.partition_id,
            "partition_name": self.partition_name,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "summary": build_summary(
                self.title, self.device_name, self.partition_name, self.user_name
            ),
            "zone": self.zone,
            "message": self.message,
            "acknowledged_at": iso(self.acknowledged_at),
            "acknowledged_by": self.acknowledged_by,
        }
        if include_children:
            data["notifications"] = [n.to_dict() for n in self.notifications]
            data["calls"] = [c.to_dict() for c in self.calls]
        return data


class NotificationLog(Base):
    """Elke poging tot een tekstmelding, geslaagd of niet.

    Falen wordt bewust bewaard en op het dashboard getoond: een alarmcentrale
    die stil faalt geeft schijnveiligheid.
    """

    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=True, index=True
    )
    channel: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(16))  # sent | failed
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    event: Mapped[Event | None] = relationship(back_populates="notifications")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel": self.channel,
            "created_at": iso(self.created_at),
            "status": self.status,
            "detail": self.detail,
        }


class CallAttempt(Base):
    """Eén belpoging richting Element X."""

    __tablename__ = "call_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=True, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    variants: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(16))  # sent | failed
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    event: Mapped[Event | None] = relationship(back_populates="calls")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "attempt": self.attempt,
            "created_at": iso(self.created_at),
            "variants": self.variants,
            "status": self.status,
            "detail": self.detail,
        }


class StateEntry(Base):
    """Kleine key/value-tabel voor status die een herstart moet overleven."""

    __tablename__ = "state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SelftestRun(Base):
    """Bewijs dat de belketen tot aan je telefoon nog werkt."""

    __tablename__ = "selftest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    kind: Mapped[str] = mapped_column(String(16), default="scheduled")  # scheduled | manual
    ring_status: Mapped[str] = mapped_column(String(16), default="pending")
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "started_at": iso(self.started_at),
            "kind": self.kind,
            "ring_status": self.ring_status,
            "acknowledged_at": iso(self.acknowledged_at),
            "detail": self.detail,
        }
