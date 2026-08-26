"""Dunne cliënt op de Matrix Client-Server API.

Bewust httpx en geen volwaardige SDK: we versturen berichten en state-events en
lezen niets terug. Een SDK met sync-lus en versleuteling zou hier alleen maar
gewicht en storingsgevoeligheid toevoegen op een Pi die 24/7 moet draaien.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx

_LOGGER = logging.getLogger(__name__)

#: Statuscodes waarbij opnieuw proberen zin heeft. Bij 4xx (verkeerd token,
#: onbekende room) verandert een herhaling niets en verspil je alleen tijd
#: terwijl er een alarm loopt.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class MatrixError(Exception):
    """Een Matrix-verzoek is definitief mislukt."""


class MatrixClient:
    def __init__(
        self,
        homeserver: str,
        token: str,
        user_id: str,
        *,
        timeout: float = 10.0,
        max_attempts: int = 4,
    ) -> None:
        self._homeserver = homeserver.rstrip("/")
        self._token = token
        self.user_id = user_id
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self._homeserver,
            timeout=self._timeout,
            headers={"Authorization": f"Bearer {self._token}"},
        )

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise MatrixError("Matrix-cliënt niet gestart")
        return self._client

    # ── Verzoeken ────────────────────────────────────────────────────────────

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._http.request(method, path, json=payload)
            except httpx.HTTPError as exc:
                last_error = exc
                _LOGGER.warning(
                    "Matrix-verzoek mislukt (poging %d/%d): %s",
                    attempt,
                    self._max_attempts,
                    exc,
                )
            else:
                if response.status_code < 300:
                    return dict(response.json())

                body = response.text[:300]
                if response.status_code not in _RETRYABLE_STATUS:
                    raise MatrixError(f"Matrix gaf {response.status_code} op {path}: {body}")

                last_error = MatrixError(f"{response.status_code}: {body}")
                if response.status_code == 429:
                    # De server vertelt zelf hoe lang we moeten wachten.
                    await asyncio.sleep(self._retry_after(response))
                    continue

            if attempt < self._max_attempts:
                await asyncio.sleep(2 ** (attempt - 1))

        raise MatrixError(f"Matrix onbereikbaar na {self._max_attempts} pogingen: {last_error}")

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        try:
            millis = response.json().get("retry_after_ms")
        except Exception:  # pragma: no cover - niet-JSON foutpagina
            millis = None
        return min(float(millis) / 1000 if millis else 2.0, 30.0)

    # ── API ──────────────────────────────────────────────────────────────────

    async def whoami(self) -> str:
        """Controleer het token en geef het bijbehorende user-ID terug."""
        data = await self._request("GET", "/_matrix/client/v3/account/whoami")
        return str(data.get("user_id", ""))

    async def send_event(
        self, room_id: str, event_type: str, content: dict[str, Any], txn_id: str
    ) -> str:
        """Stuur een bericht-event.

        Het transactie-ID komt van de aanroeper en is afgeleid van het event.
        Daardoor is een herhaling na een netwerkfout idempotent: de homeserver
        herkent hem en levert geen tweede melding af. Bij een alarm is dubbel
        bellen bijna net zo vervelend als niet bellen.
        """
        path = f"/_matrix/client/v3/rooms/{quote(room_id)}/send/{quote(event_type)}/{quote(txn_id)}"
        data = await self._request("PUT", path, content)
        return str(data.get("event_id", ""))

    async def send_state(
        self, room_id: str, event_type: str, state_key: str, content: dict[str, Any]
    ) -> str:
        path = (
            f"/_matrix/client/v3/rooms/{quote(room_id)}"
            f"/state/{quote(event_type)}/{quote(state_key, safe='')}"
        )
        data = await self._request("PUT", path, content)
        return str(data.get("event_id", ""))

    async def join_room(self, room_id: str) -> None:
        await self._request("POST", f"/_matrix/client/v3/rooms/{quote(room_id)}/join", {})

    async def get_push_rules(self) -> dict[str, Any]:
        """Haal de push-regels van het account bij dit token op."""
        return await self._request("GET", "/_matrix/client/v3/pushrules/")

    async def set_push_rule(self, rule_id: str, rule: dict[str, Any]) -> None:
        """Zet een override-push-rule op het account van het gebruikte token.

        Let op: dit werkt alleen op je eigen account. Een bot kan geen
        push-regels zetten voor iemand anders — vandaar tools/setup_pushrule.py,
        dat je met je persoonlijke token draait.
        """
        path = f"/_matrix/client/v3/pushrules/global/override/{quote(rule_id)}"
        await self._request("PUT", path, rule)
