"""Matrix als volwaardig meldkanaal: bericht, oproep en escalatie."""

from __future__ import annotations

import logging

from ...config import Config
from ...db import Database
from ...models import AlarmEvent
from ..base import NotifyError
from .client import MatrixClient, MatrixError
from .escalation import EscalationManager
from .message import build_message
from .ring import RingSender

_LOGGER = logging.getLogger(__name__)


class MatrixNotifier:
    name = "matrix"

    def __init__(self, config: Config, db: Database) -> None:
        self._config = config
        self._db = db
        matrix = config.matrix
        self._client = MatrixClient(
            homeserver=matrix.homeserver,
            token=matrix.token or "",
            user_id=matrix.user_id,
        )
        self._ring = RingSender(self._client, config)
        self.escalation = EscalationManager(config, db, self._ring)

    @property
    def client(self) -> MatrixClient:
        return self._client

    @property
    def ring_sender(self) -> RingSender:
        return self._ring

    # ── Levenscyclus ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self._client.start()
        try:
            whoami = await self._client.whoami()
        except MatrixError as exc:
            # Niet fataal: de centrale moet blijven draaien en events opslaan,
            # ook als Matrix even plat ligt. Wel luid loggen — dit betekent dat
            # er nu niet gebeld kan worden.
            _LOGGER.error(
                "Matrix-token werkt niet; er kunnen GEEN meldingen of oproepen "
                "verstuurd worden: %s",
                exc,
            )
            return

        if whoami and whoami != self._config.matrix.user_id:
            _LOGGER.warning(
                "Het token hoort bij %s, maar in config staat %s. De centrale gebruikt %s.",
                whoami,
                self._config.matrix.user_id,
                whoami,
            )
            self._client.user_id = whoami

        try:
            await self._client.join_room(self._config.matrix.room_id)
        except MatrixError as exc:
            _LOGGER.debug("Room joinen niet nodig of niet gelukt: %s", exc)

        _LOGGER.info(
            "Matrix klaar als %s in room %s; belvarianten: %s",
            whoami or self._config.matrix.user_id,
            self._config.matrix.room_id,
            ", ".join(v.name for v in self._ring.selected_variants()) or "geen",
        )

    async def stop(self) -> None:
        await self.escalation.stop()
        await self._client.stop()

    # ── Meldingen ────────────────────────────────────────────────────────────

    async def send_event(self, alarm: AlarmEvent) -> None:
        content = build_message(alarm, self._config)
        # Het transactie-ID is afgeleid van het event, dus een herhaling na een
        # netwerkfout levert geen tweede bericht op.
        txn_id = f"event-{alarm.uid}"
        try:
            await self._client.send_event(
                self._config.matrix.room_id, "m.room.message", content, txn_id
            )
        except MatrixError as exc:
            await self._db.log_notification(alarm.db_id, self.name, "failed", str(exc)[:500])
            raise NotifyError(str(exc)) from exc
        await self._db.log_notification(alarm.db_id, self.name, "sent", alarm.summary())

    async def ring_for(self, alarm: AlarmEvent) -> None:
        """Start de belronde voor dit alarm."""
        await self.escalation.start_for(alarm)

    async def test_ring(self, reason: str = "testoproep vanuit het dashboard") -> bool:
        """Eén losse oproep, om de keten tot je telefoon te bewijzen."""
        result = await self._ring.ring(reason)
        await self._db.log_call(
            None,
            1,
            ",".join(result.sent) or "-",
            "sent" if result.ok else "failed",
            result.describe(),
        )
        await self._ring.clear_membership()
        return result.ok
