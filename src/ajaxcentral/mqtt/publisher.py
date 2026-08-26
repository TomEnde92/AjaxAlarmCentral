"""Publiceer events en status naar MQTT, met Home Assistant discovery.

Twee dingen zijn hier bewust zo gebouwd:

* **Een last will.** Als de centrale omvalt of de Pi uitgaat, zet de broker het
  beschikbaarheidstopic zelf op "offline" en worden de entiteiten in Home
  Assistant grijs. Zonder dat blijft HA de laatste bekende status tonen alsof
  alles nog werkt — een dashboard dat "alles rustig" meldt terwijl er niets
  meer draait, is erger dan geen dashboard.
* **Herverbinden in een lus.** Een broker die even weg is mag de centrale niet
  meeslepen; ontvangst van alarmen is belangrijker dan MQTT.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import aiomqtt

from ..bus import EventBus
from ..config import Config
from ..models import AlarmEvent
from ..state import SystemState
from ..tasks import cancel_task

_LOGGER = logging.getLogger(__name__)

_RECONNECT_DELAY = 10.0


class MqttPublisher:
    def __init__(self, config: Config, bus: EventBus[AlarmEvent], state: SystemState) -> None:
        self._config = config
        self._bus = bus
        self._state = state
        self._task: asyncio.Task[None] | None = None
        self._client: aiomqtt.Client | None = None

    # ── Topics ───────────────────────────────────────────────────────────────

    @property
    def _base(self) -> str:
        return self._config.mqtt.base_topic

    @property
    def availability_topic(self) -> str:
        return f"{self._base}/status"

    # ── Levenscyclus ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="mqtt")

    async def stop(self) -> None:
        await cancel_task(self._task)
        self._task = None

    async def _run(self) -> None:
        settings = self._config.mqtt
        will = aiomqtt.Will(topic=self.availability_topic, payload=b"offline", qos=1, retain=True)
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=settings.host,
                    port=settings.port,
                    username=settings.username or None,
                    password=settings.password or None,
                    identifier="ajaxcentral",
                    will=will,
                ) as client:
                    self._client = client
                    _LOGGER.info("MQTT verbonden met %s:%s", settings.host, settings.port)
                    await client.publish(self.availability_topic, "online", qos=1, retain=True)
                    if settings.discovery:
                        await self._publish_discovery(client)
                    await self._publish_state(client)
                    await self._consume(client)
            except aiomqtt.MqttError as exc:
                _LOGGER.warning(
                    "MQTT-verbinding weg (%s); nieuwe poging over %.0f seconden",
                    exc,
                    _RECONNECT_DELAY,
                )
            except asyncio.CancelledError:
                await self._say_goodbye()
                raise
            finally:
                self._client = None
            await asyncio.sleep(_RECONNECT_DELAY)

    async def _say_goodbye(self) -> None:
        """Netjes offline melden bij een geplande stop, in plaats van de will."""
        if self._client is not None:
            # De broker kan al weg zijn; dan doet de last will het werk.
            with contextlib.suppress(Exception):
                await self._client.publish(self.availability_topic, "offline", qos=1, retain=True)

    async def _consume(self, client: aiomqtt.Client) -> None:
        async with self._bus.subscribe() as queue:
            while True:
                alarm = await queue.get()
                await self._publish_event(client, alarm)
                await self._publish_state(client)

    # ── Publiceren ───────────────────────────────────────────────────────────

    async def _publish_event(self, client: aiomqtt.Client, alarm: AlarmEvent) -> None:
        payload = json.dumps(alarm.to_dict(), ensure_ascii=False)
        # Niet retained: een event is een gebeurtenis, geen toestand. Retained
        # zou bij elke herstart van HA een oud alarm opnieuw laten afgaan.
        await client.publish(f"{self._base}/event", payload, qos=1, retain=False)

    async def _publish_state(self, client: aiomqtt.Client) -> None:
        state = self._state
        await client.publish(
            f"{self._base}/system/hub",
            "online" if state.hub_online else "offline",
            qos=1,
            retain=True,
        )
        await client.publish(
            f"{self._base}/system/alarm",
            "ON" if state.open_alarms else "OFF",
            qos=1,
            retain=True,
        )
        await client.publish(
            f"{self._base}/system/trouble",
            "ON" if state.troubles else "OFF",
            qos=1,
            retain=True,
        )
        await client.publish(
            f"{self._base}/system/attributes",
            json.dumps(state.to_dict(), ensure_ascii=False),
            qos=1,
            retain=True,
        )
        for partition in state.partitions.values():
            await client.publish(
                f"{self._base}/partition/{partition.partition_id}/state",
                "armed" if partition.armed else "disarmed",
                qos=1,
                retain=True,
            )

    # ── Home Assistant discovery ─────────────────────────────────────────────

    def _device(self) -> dict[str, Any]:
        return {
            "identifiers": ["ajaxcentral"],
            "name": "Ajax Alarmcentrale",
            "manufacturer": "zelfbouw",
            "model": "SIA DC-09 ontvanger",
        }

    async def _publish_discovery(self, client: aiomqtt.Client) -> None:
        """Meld de entiteiten aan bij Home Assistant.

        Retained, zodat HA ze na een herstart terugvindt zonder dat de centrale
        opnieuw op hoeft te starten.
        """
        prefix = self._config.mqtt.discovery_prefix
        device = self._device()
        entities: list[tuple[str, str, dict[str, Any]]] = [
            (
                "binary_sensor",
                "hub",
                {
                    "name": "Hub verbonden",
                    "state_topic": f"{self._base}/system/hub",
                    "payload_on": "online",
                    "payload_off": "offline",
                    "device_class": "connectivity",
                },
            ),
            (
                "binary_sensor",
                "alarm",
                {
                    "name": "Alarm actief",
                    "state_topic": f"{self._base}/system/alarm",
                    "device_class": "safety",
                    "json_attributes_topic": f"{self._base}/system/attributes",
                },
            ),
            (
                "binary_sensor",
                "trouble",
                {
                    "name": "Storing",
                    "state_topic": f"{self._base}/system/trouble",
                    "device_class": "problem",
                },
            ),
        ]

        for partition in self._state.partitions.values():
            entities.append(
                (
                    "sensor",
                    f"partition_{partition.partition_id}",
                    {
                        "name": f"Groep {partition.name}",
                        "state_topic": (f"{self._base}/partition/{partition.partition_id}/state"),
                    },
                )
            )

        for component, object_id, config in entities:
            config.update(
                {
                    "unique_id": f"ajaxcentral_{object_id}",
                    "availability_topic": self.availability_topic,
                    "payload_available": "online",
                    "payload_not_available": "offline",
                    "device": device,
                }
            )
            await client.publish(
                f"{prefix}/{component}/ajaxcentral/{object_id}/config",
                json.dumps(config, ensure_ascii=False),
                qos=1,
                retain=True,
            )
        _LOGGER.info("Home Assistant discovery gepubliceerd (%d entiteiten)", len(entities))
