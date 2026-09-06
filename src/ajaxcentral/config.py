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

#: Alarmcategorieën die standaard een noodmelding opleveren, en niet alleen een
#: bericht.
DEFAULT_RING_CATEGORIES: tuple[str, ...] = (
    "burglary",
    "fire",
    "gas",
    "heat",
    "panic",
    "medical",
    "supervision",
)

ENV_PREFIX = "AJAXCENTRAL_"


#: Soorten melder die je per apparaat kunt instellen. "other" is voor alles wat
#: geen alarm geeft (sirene, sleutelhanger, bedienpaneel) en dus bij geen enkel
#: testalarm te kiezen is.
DEVICE_TYPES: tuple[str, ...] = ("fire", "burglary", "other")


class SiaConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 10000
    protocol: Literal["tcp", "udp"] = "tcp"
    account_id: str = "AA01"
    key: str | None = None
    ping_interval_seconds: int = 60
    offline_factor: float = 2.5
    #: Zolang de hub weg blijft en het HUBOFF-alarm niet bevestigd is, wordt
    #: het om de zoveel seconden opnieuw gemeld (nieuwe belronde). 0 = uit.
    offline_repeat_seconds: int = 900

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


class PushoverConfig(BaseModel):
    """Pushover: noodmelding met bevestiging over en weer."""

    enabled: bool = False
    user_key: str | None = None
    token: str | None = None
    #: Leeg = alle toestellen van het account.
    device: str | None = None
    #: Geluid van de noodmelding; zie pushover.net/api#sounds.
    sound: str = "siren"
    #: Pushover herhaalt de noodmelding op de telefoon met dit interval (min. 30)
    #: tot hij bevestigd is, en geeft het op na expire_seconds (max. 10800).
    #: Daarna sturen we zelf een nieuwe noodmelding, tot iemand bevestigt.
    retry_seconds: int = 30
    expire_seconds: int = 10800
    #: Welke alarmcategorieën een noodmelding zijn; de rest is een gewoon bericht.
    categories: list[str] = Field(default_factory=lambda: list(DEFAULT_RING_CATEGORIES))
    #: Hoe vaak we bij Pushover navragen of de noodmelding bevestigd is.
    poll_interval_seconds: float = 20.0
    api_url: str = "https://api.pushover.net/1"

    @field_validator("retry_seconds")
    @classmethod
    def _check_retry(cls, v: int) -> int:
        if v < 30:
            raise ValueError("pushover.retry_seconds moet minimaal 30 zijn (eis van Pushover)")
        return v

    @field_validator("expire_seconds")
    @classmethod
    def _check_expire(cls, v: int) -> int:
        if not 30 <= v <= 10800:
            raise ValueError("pushover.expire_seconds moet tussen 30 en 10800 liggen")
        return v

    @model_validator(mode="after")
    def _check_complete(self) -> PushoverConfig:
        if self.enabled and not (self.user_key and self.token):
            raise ValueError(
                "pushover.enabled staat aan maar AJAXCENTRAL_PUSHOVER_USER en/of "
                "AJAXCENTRAL_PUSHOVER_TOKEN ontbreekt in .env"
            )
        return self


def _in_window(start: time, end: time, now: time) -> bool:
    """Valt `now` in het venster [start, end)? Een venster mag over middernacht lopen."""
    if start <= end:
        return start <= now < end
    return now >= start or now < end


class ArmingConfig(BaseModel):
    """Hoe in- en uitschakelmeldingen benoemd worden.

    Ajax kent een "nachtmodus" waarin alleen de melders van de nachtgroep
    aangaan, en stuurt daarvoor altijd de code NL — ook als je hem overdag
    gebruikt om alleen de begane grond in te schakelen. Binnen het nachtvenster
    heet dat hier "Nachtinschakeling", daarbuiten "Deelinschakeling".
    """

    night_start: time = time(21, 0)
    night_end: time = time(7, 0)
    night_title: str = "Nachtinschakeling"
    partial_title: str = "Deelinschakeling"

    def is_night(self, now: time) -> bool:
        return _in_window(self.night_start, self.night_end, now)

    def title(self, now: time, *, forced: bool = False) -> str:
        base = self.night_title if self.is_night(now) else self.partial_title
        return f"{base} (geforceerd)" if forced else base


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
        return _in_window(self.start, self.end, now)


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
    #: Tijdzone voor tijdstippen in meldingen. Het dashboard gebruikt de
    #: tijdzone van je browser en heeft dit niet nodig.
    timezone: str = "Europe/Amsterdam"
    sia: SiaConfig = Field(default_factory=SiaConfig)
    devices: dict[str, str] = Field(default_factory=dict)
    #: Soort melder per apparaat-id: fire, burglary of other. Bepaalt welke
    #: melders je bij een testalarm mag kiezen. Niet genoemd = onbekend.
    device_types: dict[str, str] = Field(default_factory=dict)
    partitions: dict[str, str] = Field(default_factory=dict)
    users: dict[str, str] = Field(default_factory=dict)
    arming: ArmingConfig = Field(default_factory=ArmingConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    pushover: PushoverConfig = Field(default_factory=PushoverConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    selftest: SelftestConfig = Field(default_factory=SelftestConfig)
    mqtt: MqttConfig = Field(default_factory=MqttConfig)

    @field_validator("device_types")
    @classmethod
    def _check_device_types(cls, v: dict[str, str]) -> dict[str, str]:
        for device_id, kind in v.items():
            if kind not in DEVICE_TYPES:
                raise ValueError(
                    f"device_types[{device_id!r}] is {kind!r}; kies uit {', '.join(DEVICE_TYPES)}"
                )
        return v

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

    def device_type(self, device_id: str | None) -> str | None:
        """Soort melder (fire, burglary, other), of None als het niet is ingesteld."""
        if not device_id:
            return None
        return self.device_types.get(device_id) or self.device_types.get(device_id.lstrip("0"))

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
    put("pushover", "user_key", "PUSHOVER_USER")
    put("pushover", "token", "PUSHOVER_TOKEN")
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
    for section in ("devices", "device_types", "partitions", "users"):
        if raw.get(section):
            raw[section] = {str(k): str(v) for k, v in raw[section].items()}
    return Config.model_validate(_apply_secrets(raw))
