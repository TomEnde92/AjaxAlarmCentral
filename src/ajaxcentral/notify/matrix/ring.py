"""De rinkelende oproep naar Element X.

Dit is het meest onzekere onderdeel van de hele centrale, en dat is geen
implementatiedetail maar een eigenschap van het Matrix-ecosysteem op dit moment:

* Element X ondersteunt geen klassieke 1-op-1 VoIP meer (`m.call.invite`).
  Rinkelen loopt via MatrixRTC / Element Call.
* Het event dat daarvoor zorgt komt uit **MSC4075**, dat nog niet stabiel is.
  Het heette eerst `m.call.notify` met veld `notify_type`, en is later
  `m.rtc.notification` geworden. Welke variant jouw Element X-build herkent,
  is een empirische vraag.
* Op Android is rinkelen aantoonbaar wisselvallig: element-x-android#4390 staat
  open met label *major severity* — rings in DM-rooms komen soms niet of pas
  na minuten aan.

Daarom staat de payload hier als **data en niet als code**: elke variant is een
apart recept, en `tools/ringtest.py` vuurt ze los af zodat je zelf vaststelt
welke bij jou werkt. Verandert de MSC, dan pas je een recept aan in plaats van
de belcode.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ...config import Config, RingConfig
from .client import MatrixClient, MatrixError

_LOGGER = logging.getLogger(__name__)

#: MatrixRTC-lidmaatschap (MSC4143). Waarschijnlijk rinkelt een client alleen
#: als er ook echt een call-sessie loopt; dit meldt de bot als deelnemer aan.
#: Zolang er geen LiveKit-stack achter zit is dat lidmaatschap leeg — je hoort
#: niets als je opneemt. Dat is bewust de eerste stap: het bewijst dat je
#: telefoon gaat, en het is precies de haak waar een latere spraakbot in klikt.
MEMBER_EVENT_TYPES = ("m.rtc.member", "org.matrix.msc3401.call.member")


@dataclass(frozen=True, slots=True)
class RingVariant:
    """Eén manier om te proberen de telefoon te laten rinkelen."""

    name: str
    event_type: str
    description: str

    def build(self, config: Config, call_id: str) -> dict[str, Any]:
        ring = config.matrix.ring
        target = config.matrix.target_user_id
        mentions = {"user_ids": [target] if target else [], "room": not target}

        if self.name.startswith("call-notify"):
            # De oorspronkelijke MSC4075-vorm, zoals Element Call hem stuurt.
            return {
                "application": "m.call",
                "call_id": call_id,
                "notify_type": "ring",
                "m.mentions": mentions,
                "lifetime": ring.lifetime_ms,
            }

        # De hernoemde vorm. We zetten zowel `notification_type` als
        # `notify_type` en zowel `intent` als `m.call.intent`: de veldnamen zijn
        # tijdens de MSC verschoven en een extra veld dat een client niet kent
        # wordt genegeerd, terwijl een ontbrekend veld het rinkelen kost.
        return {
            "m.mentions": mentions,
            "notification_type": "ring",
            "notify_type": "ring",
            "lifetime": ring.lifetime_ms,
            "sender_ts": int(time.time() * 1000),
            "intent": ring.intent,
            "m.call.intent": ring.intent,
            "call_id": call_id,
        }


VARIANTS: dict[str, RingVariant] = {
    "rtc-notification": RingVariant(
        name="rtc-notification",
        event_type="m.rtc.notification",
        description="De hernoemde MSC4075-vorm; de stabiele naam waar het heen gaat.",
    ),
    "rtc-notification-unstable": RingVariant(
        name="rtc-notification-unstable",
        event_type="org.matrix.msc4075.rtc.notification",
        description="Zelfde inhoud, onstabiele naam. Nodig voor builds die de MSC volgen.",
    ),
    "call-notify-legacy": RingVariant(
        name="call-notify-legacy",
        event_type="m.call.notify",
        description="De oorspronkelijke vorm; oudere Element-versies kennen alleen deze.",
    ),
    "call-notify-unstable": RingVariant(
        name="call-notify-unstable",
        event_type="org.matrix.msc4075.call.notify",
        description="Onstabiele naam van de oorspronkelijke vorm.",
    ),
}


class RingResult:
    """Uitkomst van één belpoging over alle varianten samen."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.failed: dict[str, str] = {}

    @property
    def ok(self) -> bool:
        """Geslaagd zodra één variant is aangekomen.

        Bewust niet "alle varianten geslaagd": we sturen er meerdere juist
        omdat we niet weten welke jouw client begrijpt, dus dat er een paar
        afketsen is de verwachting en geen storing.
        """
        return bool(self.sent)

    def describe(self) -> str:
        parts = []
        if self.sent:
            parts.append("verstuurd: " + ", ".join(self.sent))
        if self.failed:
            parts.append("mislukt: " + ", ".join(f"{k} ({v})" for k, v in self.failed.items()))
        return "; ".join(parts) or "niets verstuurd"


class RingSender:
    def __init__(self, client: MatrixClient, config: Config) -> None:
        self._client = client
        self._config = config

    @property
    def _ring(self) -> RingConfig:
        return self._config.matrix.ring

    def selected_variants(self) -> list[RingVariant]:
        chosen = []
        for name in self._ring.variants:
            variant = VARIANTS.get(name)
            if variant is None:
                _LOGGER.warning(
                    "Onbekende ring-variant %r in config; bekend zijn: %s",
                    name,
                    ", ".join(sorted(VARIANTS)),
                )
                continue
            chosen.append(variant)
        return chosen

    async def ring(self, reason: str, *, call_id: str | None = None) -> RingResult:
        """Laat de telefoon rinkelen. Geeft terug wat er gelukt is."""
        room_id = self._config.matrix.room_id
        call_id = call_id or uuid.uuid4().hex
        result = RingResult()

        if self._ring.with_member_state:
            await self._announce_membership(room_id, call_id)

        for variant in self.selected_variants():
            content = variant.build(self._config, call_id)
            txn_id = f"ring-{call_id}-{variant.name}"
            try:
                await self._client.send_event(room_id, variant.event_type, content, txn_id)
            except MatrixError as exc:
                result.failed[variant.name] = str(exc)[:120]
                _LOGGER.warning("Ring-variant %s mislukt: %s", variant.name, exc)
            else:
                result.sent.append(variant.name)
                _LOGGER.info("Ring-variant %s verstuurd (%s)", variant.name, reason)

        if not result.ok:
            _LOGGER.error("Geen enkele ring-variant kwam aan: %s", result.describe())
        return result

    async def _announce_membership(self, room_id: str, call_id: str) -> None:
        """Meld de bot aan als deelnemer aan de call-sessie."""
        content = {
            "application": "m.call",
            "call_id": call_id,
            "scope": "m.room",
            "device_id": "AJAXCENTRAL",
            "expires": self._ring.lifetime_ms,
            "foci_preferred": [],
        }
        await self._set_membership(room_id, content)

    async def clear_membership(self) -> None:
        """Beëindig de sessie door het lidmaatschap leeg te maken.

        Zonder dit blijft er in de room een call hangen die niemand kan
        beantwoorden, en zien clients daarna mogelijk een 'gesprek bezig' waar
        er geen is.
        """
        await self._set_membership(self._config.matrix.room_id, {})

    async def _set_membership(self, room_id: str, content: dict[str, Any]) -> None:
        user_id = self._config.matrix.user_id
        last_error: MatrixError | None = None
        for event_type in MEMBER_EVENT_TYPES:
            try:
                await self._client.send_state(room_id, event_type, user_id, content)
            except MatrixError as exc:
                last_error = exc
            else:
                return
        if last_error is not None:
            # Geen reden om het bellen af te blazen: het lidmaatschap is een
            # hulpmiddel, het ring-event is het eigenlijke signaal.
            _LOGGER.warning("Kon MatrixRTC-lidmaatschap niet zetten: %s", last_error)
