"""Ontvangst van SIA DC-09 berichten van de Ajax hub.

Het protocol zelf — frame-opbouw, CRC-16/ARC, AES-128 ontsleuteling, ACK/NAK —
laten we aan pysiaalarm over. Deze module doet drie dingen die de bibliotheek
niet doet: vertalen naar ons domeinmodel, ruwe frames bewaren voor diagnostiek,
en bijhouden wanneer de hub voor het laatst van zich liet horen.
"""

from __future__ import annotations

import dataclasses
import logging
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pysiaalarm import CommunicationsProtocol, SIAAccount, SIAEvent
from pysiaalarm.aio import SIAClient

from .config import Config
from .models import AlarmEvent, iso, utcnow
from .normalize import normalize

_LOGGER = logging.getLogger(__name__)


class Receiver:
    """Draait de SIA-server en levert genormaliseerde events af."""

    def __init__(
        self,
        config: Config,
        on_event: Callable[[AlarmEvent], None],
        *,
        raw_log_size: int = 50,
    ) -> None:
        self._config = config
        self._on_event = on_event
        self._client: SIAClient | None = None
        self.last_contact: datetime | None = None
        #: Laatste ruwe frames, inclusief geweigerde. Het dashboard toont ze
        #: onder Diagnostiek — bij het inregelen van de hub is dit het enige
        #: dat laat zien of er überhaupt iets binnenkomt.
        self.raw_log: deque[dict[str, Any]] = deque(maxlen=raw_log_size)

    # ── Levenscyclus ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        sia = self._config.sia
        account = SIAAccount(account_id=sia.account_id, key=sia.key)
        protocol = (
            CommunicationsProtocol.UDP if sia.protocol == "udp" else CommunicationsProtocol.TCP
        )
        # SIAClient kiest via __new__ zelf de TCP- of UDP-subklasse; voor een
        # typechecker ziet de basisklasse er daardoor abstract uit.
        self._client = SIAClient(  # type: ignore[abstract]
            host=sia.host,
            port=sia.port,
            accounts=[account],
            function=self._handle,
            protocol=protocol,
        )
        self._install_raw_tap()
        await self._client.async_start()

        if not sia.key:
            _LOGGER.warning(
                "SIA-verkeer is ONVERSLEUTELD. Iedereen op je netwerk kan meelezen "
                "en alarmen vervalsen. Zet AJAXCENTRAL_SIA_KEY en dezelfde sleutel "
                "in de Ajax-app."
            )
        _LOGGER.info(
            "SIA-ontvangst gestart op %s:%s (%s), account %s, versleuteld: %s",
            sia.host,
            sia.port,
            sia.protocol.upper(),
            sia.account_id,
            "ja" if sia.key else "nee",
        )

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.async_stop()
            self._client = None
            _LOGGER.info("SIA-ontvangst gestopt")

    # ── Diagnostiek ──────────────────────────────────────────────────────────

    def _install_raw_tap(self) -> None:
        """Log elk binnenkomend frame, ook de frames die geweigerd worden.

        pysiaalarm roept onze callback alleen aan voor geldige, geaccepteerde
        events. Juist bij het inregelen wil je de afgekeurde berichten zien:
        een verkeerd objectnummer of een niet-overeenkomende sleutel is anders
        volstrekt onzichtbaar, en dat is exact het geval waarin je zit te
        zoeken waarom er niets binnenkomt.
        """
        assert self._client is not None
        server = self._client.sia_server
        assert server is not None, "server bestaat pas na het aanmaken van de client"
        original = server.parse_and_check_event

        def tap(data: bytes) -> Any:
            line = data.decode("ascii", errors="ignore").strip()
            event = original(data)
            response = getattr(getattr(event, "response", None), "value", None)
            self.raw_log.appendleft(
                {
                    "at": iso(utcnow()),
                    "line": line,
                    "response": response or "geen",
                    "accepted": response == "ACK",
                }
            )
            if response and response != "ACK":
                _LOGGER.warning(
                    "Bericht geweigerd (%s). Controleer objectnummer en "
                    "encryptiesleutel in de Ajax-app. Ruw: %s",
                    response,
                    line,
                )
            return event

        server.parse_and_check_event = tap  # type: ignore[method-assign]

    @property
    def counts(self) -> dict[str, int]:
        """Tellers van pysiaalarm: hoeveel berichten om welke reden afvielen.

        Dit is de snelste manier om te zien waaróm er niets doorkomt: staat
        error_account hoog, dan klopt het objectnummer niet; error_crc wijst op
        een verkeerde encryptiesleutel.
        """
        if self._client is None:
            return {}
        return dataclasses.asdict(self._client.counts)

    # ── Verwerking ───────────────────────────────────────────────────────────

    async def _handle(self, event: SIAEvent) -> None:
        """Callback van pysiaalarm, per geaccepteerd event.

        De ACK is op dit moment al verstuurd, dus we vertragen de hub hier
        niet. We houden het toch kort: normaliseren en doorgeven, en de
        opslag en meldingen gebeuren verderop in de pijplijn.
        """
        self.last_contact = utcnow()
        try:
            alarm = normalize(event, self._config)
        except Exception:
            _LOGGER.exception("Kon binnengekomen event niet vertalen: %s", event)
            return
        _LOGGER.debug("Event ontvangen: %s", alarm.summary())
        self._on_event(alarm)
