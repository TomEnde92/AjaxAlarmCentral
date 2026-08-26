"""Configuratie: YAML-bestand voor instellingen, .env voor secrets.

Secrets staan bewust nooit in de YAML, zodat config.yaml gedeeld of in een
repo gezet kan worden zonder dat er sleutels uitlekken.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

Severity = Literal["info", "heartbeat", "restore", "trouble", "alarm", "unknown"]

#: Volgorde van laag naar hoog. Gebruikt om `min_severity` te vergelijken.
SEVERITY_ORDER: dict[str, int] = {
    "heartbeat": 0,
    "info": 1,
    "restore": 2,
    "unknown": 3,
    "trouble": 4,
    "alarm": 5,
}

ENV_PREFIX = "AJAXCENTRAL_"


class SiaConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 10000
    protocol: Literal["tcp", "udp"] = "tcp"
    account_id: str = "AA01"
    key: str | None = None
    ping_interval_seconds: int = 60
    offline_factor: float = 2.5

    @property
    def offline_after_seconds(self) -> float:
        return self.ping_interval_seconds * self.offline_factor

    @field_validator("account_id")
    @classmethod
    def _check_account(cls, v: str) -> str:
        if not 3 <= len(v) <= 16:
            raise ValueError("account_id moet 3 tot 16 tekens zijn (SIA DC-09 eis)")
        return v.upper()

    @field_validator("key")
    @classmethod
    def _check_key(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        if len(v) not in (16, 24, 32):
            raise ValueError(
                f"encryptiesleutel moet 16, 24 of 32 tekens zijn, kreeg {len(v)}. "
                "Dit is een AES-eis; dezelfde sleutel moet in de Ajax-app staan."
            )
        return v


class DatabaseConfig(BaseModel):
    path: Path = Path("data/ajaxcentral.db")
    retention_days: int = 730


class WebConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    username: str = "admin"
    password_hash: str | None = None
    secret: str | None = None
    base_url: str = "http://localhost:8080"

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")


class RingConfig(BaseModel):
    enabled: bool = True
    variants: list[str] = Field(
        default_factory=lambda: [
            "rtc-notification",
            "rtc-notification-unstable",
            "call-notify-legacy",
        ]
    )
    with_member_state: bool = True
    lifetime_ms: int = 45000
    intent: Literal["voice", "video"] = "voice"
    categories: list[str] = Field(
        default_factory=lambda: [
            "burglary",
            "fire",
            "gas",
            "heat",
            "panic",
            "medical",
            "supervision",
        ]
    )
    # Float zodat tests met fracties kunnen werken zonder een minuut te wachten.
    retry_interval_seconds: float = 60.0
    max_attempts: int = 5


class MatrixConfig(BaseModel):
    enabled: bool = False
    homeserver: str = ""
    user_id: str = ""
    room_id: str = ""
    target_user_id: str = ""
    token: str | None = None
    ring: RingConfig = Field(default_factory=RingConfig)

    @field_validator("homeserver")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @model_validator(mode="after")
    def _check_complete(self) -> MatrixConfig:
        if not self.enabled:
            return self
        missing = [
            name
            for name in ("homeserver", "user_id", "room_id", "target_user_id")
            if not getattr(self, name)
        ]
        if missing:
            raise ValueError(
                "matrix.enabled staat aan maar deze velden ontbreken in config.yaml: "
                + ", ".join(missing)
            )
        if not self.token:
            raise ValueError(
                "matrix.enabled staat aan maar AJAXCENTRAL_MATRIX_TOKEN ontbreekt in .env"
            )
        return self


class QuietHoursConfig(BaseModel):
    enabled: bool = False
    start: time = time(23, 0)
    end: time = time(7, 0)
    allow_severities: list[str] = Field(default_factory=lambda: ["alarm"])

    @model_validator(mode="after")
    def _alarm_always_allowed(self) -> QuietHoursConfig:
        # Bewuste hardcoded regel: stille uren mogen nooit een inbraak- of
        # brandmelding onderdrukken. Wie dat wil, zet de hele melding uit.
        if "alarm" not in self.allow_severities:
            self.allow_severities = [*self.allow_severities, "alarm"]
        return self

    def is_quiet(self, now: time) -> bool:
        if not self.enabled:
            return False
        if self.start <= self.end:
            return self.start <= now < self.end
        # Venster loopt over middernacht heen.
        return now >= self.start or now < self.end


class NotificationsConfig(BaseModel):
    min_severity: Severity = "trouble"
    dedupe_window_seconds: int = 30
    quiet_hours: QuietHoursConfig = Field(default_factory=QuietHoursConfig)


class SelftestConfig(BaseModel):
    enabled: bool = True
    weekday: int = 1
    hour: int = 14
    minute: int = 0
    ack_deadline_minutes: int = 30

    @field_validator("weekday")
    @classmethod
    def _check_weekday(cls, v: int) -> int:
        if not 0 <= v <= 6:
            raise ValueError("weekday moet 0 (maandag) t/m 6 (zondag) zijn")
        return v


class MqttConfig(BaseModel):
    enabled: bool = False
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    base_topic: str = "ajaxcentral"
    discovery: bool = True
    discovery_prefix: str = "homeassistant"


class Config(BaseModel):
    #: Tijdzone voor tijdstippen in Matrix-berichten. Het dashboard gebruikt de
    #: tijdzone van je browser en heeft dit niet nodig.
    timezone: str = "Europe/Amsterdam"
    sia: SiaConfig = Field(default_factory=SiaConfig)
    devices: dict[str, str] = Field(default_factory=dict)
    partitions: dict[str, str] = Field(default_factory=dict)
    users: dict[str, str] = Field(default_factory=dict)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    matrix: MatrixConfig = Field(default_factory=MatrixConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    selftest: SelftestConfig = Field(default_factory=SelftestConfig)
    mqtt: MqttConfig = Field(default_factory=MqttConfig)

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as exc:
            raise ValueError(f"onbekende tijdzone {v!r}") from exc
        return v

    def to_local(self, moment: datetime) -> datetime:
        """Reken een UTC-tijdstip om naar de ingestelde tijdzone."""
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(ZoneInfo(self.timezone))

    def device_name(self, device_id: str | None) -> str:
        if not device_id:
            return "systeem"
        return (
            self.devices.get(device_id)
            or self.devices.get(device_id.lstrip("0"))
            or (f"apparaat {device_id}")
        )

    def user_name(self, user_id: str | None) -> str:
        if not user_id:
            return "onbekende gebruiker"
        return (
            self.users.get(user_id) or self.users.get(user_id.lstrip("0")) or f"gebruiker {user_id}"
        )

    def partition_name(self, partition_id: str | None) -> str:
        if not partition_id:
            return "systeem"
        return (
            self.partitions.get(partition_id)
            or self.partitions.get(partition_id.lstrip("0"))
            or f"groep {partition_id}"
        )


def _apply_secrets(raw: dict[str, Any]) -> dict[str, Any]:
    """Vul secrets uit environment-variabelen in.

    De YAML bevat ze bewust niet; zo blijft config.yaml deelbaar.
    """

    def put(section: str, key: str, env: str) -> None:
        value = os.environ.get(ENV_PREFIX + env)
        if value:
            raw.setdefault(section, {})[key] = value

    put("sia", "key", "SIA_KEY")
    put("web", "password_hash", "WEB_PASSWORD_HASH")
    put("web", "secret", "WEB_SECRET")
    put("matrix", "token", "MATRIX_TOKEN")
    put("mqtt", "username", "MQTT_USERNAME")
    put("mqtt", "password", "MQTT_PASSWORD")
    return raw


def load_config(path: str | Path | None = None) -> Config:
    """Lees config.yaml (indien aanwezig) en meng de secrets uit .env erdoorheen."""
    candidate = Path(path) if path else Path(os.environ.get(ENV_PREFIX + "CONFIG", "config.yaml"))
    raw: dict[str, Any] = {}
    if candidate.exists():
        loaded = yaml.safe_load(candidate.read_text(encoding="utf-8"))
        if loaded:
            raw = loaded
    # YAML mag getallen als sleutel hebben; wij indexeren op string.
    for section in ("devices", "partitions", "users"):
        if raw.get(section):
            raw[section] = {str(k): str(v) for k, v in raw[section].items()}
    return Config.model_validate(_apply_secrets(raw))
