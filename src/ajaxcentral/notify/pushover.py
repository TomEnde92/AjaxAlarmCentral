"""Pushover als meldkanaal: bericht, noodmelding en bevestiging over en weer.

Eén app op je telefoon en één HTTPS-verzoek per melding. Een alarm gaat als
*noodmelding* (prioriteit 2): een hard geluid
dat door Niet Storen heen gaat en dat Pushover zelf blijft herhalen tot je in
de app op Bevestigen tikt.

Wat dit kanaal bruikbaar maakt voor een alarmcentrale is de terugkoppeling.
Pushover geeft bij een noodmelding een ontvangstbewijs (receipt) terug waarmee
we kunnen navragen óf en waar het alarm bevestigd is. Die bevestiging zetten we
in de database, zodat het dashboard het ziet. Andersom geldt hetzelfde:
bevestig je in het dashboard, dan trekken we het bewijs in en houdt de telefoon
op met herhalen.

Pushover geeft een noodmelding na hooguit drie uur op. Een alarm mag niet
stilvallen omdat niemand het gehoord heeft, dus zodra een noodmelding verloopt
zonder bevestiging sturen we een nieuwe, en dat blijft doorgaan tot iemand het
alarm bevestigt — als een pieper van de brandweer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ..config import Config
from ..db import Database
from ..models import AlarmEvent
from ..tasks import cancel_task
from .base import NotifyError
from .text import event_link, plain_text

_LOGGER = logging.getLogger(__name__)

#: Sleutel in de state-tabel: {event_id | "selftest": receipt}. Zo overleeft
#: een lopende noodmelding een herstart en volgen we hem daarna gewoon verder.
_STATE_KEY = "pushover_receipts"
_SELFTEST_KEY = "selftest"
_SELFTEST_EXPIRE_SECONDS = 600
#: Wachttijd voordat we een verlopen noodmelding opnieuw proberen te sturen als
#: Pushover het verzoek weigerde of onbereikbaar was.
_RESEND_RETRY_SECONDS = 60


class PushoverError(Exception):
    """Pushover accepteerde het verzoek niet, of was niet bereikbaar."""


class PushoverNotifier:
    name = "pushover"

    def __init__(
        self,
        config: Config,
        db: Database,
        *,
        on_acknowledged: Callable[[int], None] | None = None,
    ) -> None:
        self._config = config
        self._settings = config.pushover
        self._db = db
        #: Wordt aangeroepen als een alarm op de telefoon bevestigd is, zodat
        #: andere kanalen kunnen stoppen.
        self._on_acknowledged = on_acknowledged
        #: Gezet door main zodra de zelftest bestaat; sluit de testlus.
        self.on_selftest_acknowledged: Callable[[str], Awaitable[Any]] | None = None
        self._client: httpx.AsyncClient | None = None
        self._receipts: dict[str, str] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._background: set[asyncio.Task[None]] = set()

    # ── Levenscyclus ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._settings.api_url, timeout=20)
        try:
            data = await self._post("/users/validate.json", {})
        except PushoverError as exc:
            # Niet fataal: de centrale moet blijven draaien en events opslaan.
            # Wel luid, want dit betekent dat er nu niets via Pushover uitgaat.
            _LOGGER.error(
                "Pushover-sleutels werken niet; er gaan GEEN meldingen via Pushover uit: %s",
                exc,
            )
        else:
            devices = data.get("devices") or []
            _LOGGER.info("Pushover klaar; toestellen: %s", ", ".join(devices) or "geen")

        stored = await self._db.get_state(_STATE_KEY, {})
        self._receipts = dict(stored) if isinstance(stored, dict) else {}
        for key, receipt in list(self._receipts.items()):
            self._watch(key, receipt)

    async def stop(self) -> None:
        for task in list(self._watchers.values()):
            await cancel_task(task)
        self._watchers.clear()
        for task in list(self._background):
            await cancel_task(task)
        self._background.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Meldingen ────────────────────────────────────────────────────────────

    async def send_event(self, alarm: AlarmEvent) -> None:
        await self._send(alarm, attempt=1)

    async def _send(self, alarm: AlarmEvent, *, attempt: int) -> None:
        """Stuur het bericht; attempt > 1 is een herhaalde noodmelding."""
        title, body = plain_text(alarm, self._config)
        if attempt > 1:
            body = f"{body}\n\nNog steeds niet bevestigd; noodmelding {attempt}."
        payload: dict[str, Any] = {
            "title": title,
            "message": body,
            "url": event_link(alarm, self._config),
            "url_title": "Open het dashboard",
        }
        if self._settings.device:
            payload["device"] = self._settings.device

        emergency = alarm.is_alarm and alarm.category in self._settings.categories
        if emergency:
            payload.update(
                priority=2,
                retry=self._settings.retry_seconds,
                expire=self._settings.expire_seconds,
                sound=self._settings.sound,
            )
        elif alarm.severity == "trouble":
            # Hoog: rood, en door de stille uren van Pushover heen.
            payload["priority"] = 1
        else:
            payload["priority"] = 0

        try:
            data = await self._post("/messages.json", payload, attempts=3 if alarm.is_alarm else 1)
        except PushoverError as exc:
            await self._db.log_notification(alarm.db_id, self.name, "failed", str(exc)[:500])
            raise NotifyError(str(exc)) from exc
        await self._db.log_notification(alarm.db_id, self.name, "sent", alarm.summary())

        receipt = data.get("receipt")
        if emergency and receipt and alarm.db_id is not None:
            await self._db.log_call(
                alarm.db_id,
                attempt,
                "pushover",
                "sent",
                f"noodmelding; de telefoon herhaalt elke {self._settings.retry_seconds}s "
                f"tot bevestiging, na {self._settings.expire_seconds}s volgt een nieuwe",
            )
            key = str(alarm.db_id)
            await self._remember(key, receipt)
            self._watch(key, receipt)

    async def resume_open_alarms(self) -> int:
        """Stuur na een herstart opnieuw een noodmelding voor open alarmen.

        Een alarm waarvan het ontvangstbewijs nog gevolgd wordt, slaan we over:
        dat herhaalt Pushover zelf nog.
        """
        resumed = 0
        for row in await self._db.unacknowledged_alarms():
            if row.severity != "alarm" or row.category not in self._settings.categories:
                continue
            if str(row.id) in self._watchers:
                continue
            try:
                await self.send_event(AlarmEvent.from_row(row))
            except NotifyError:
                continue
            resumed += 1
        if resumed:
            _LOGGER.warning(
                "%d openstaand alarm(en) opnieuw via Pushover gemeld na herstart", resumed
            )
        return resumed

    async def test_ring(self, reason: str = "testoproep vanuit het dashboard") -> bool:
        """Eén noodmelding om te bewijzen dat de keten tot je telefoon werkt.

        Bevestig je hem in de Pushover-app, dan wordt de zelftest vanzelf als
        bevestigd vastgelegd; je hoeft dan niet ook nog naar het dashboard.
        """
        payload: dict[str, Any] = {
            "title": "Testmelding van de alarmcentrale",
            "message": reason,
            "priority": 2,
            "retry": max(30, min(self._settings.retry_seconds, 60)),
            "expire": _SELFTEST_EXPIRE_SECONDS,
            "sound": self._settings.sound,
            "url": self._config.web.base_url,
            "url_title": "Open het dashboard",
        }
        if self._settings.device:
            payload["device"] = self._settings.device
        try:
            data = await self._post("/messages.json", payload, attempts=2)
        except PushoverError as exc:
            await self._db.log_call(None, 1, "pushover", "failed", str(exc)[:500])
            return False
        await self._db.log_call(None, 1, "pushover", "sent", "testmelding via Pushover")
        receipt = data.get("receipt")
        if receipt:
            await self._remember(_SELFTEST_KEY, receipt)
            self._watch(_SELFTEST_KEY, receipt)
        return True

    def cancel_for(self, event_id: int) -> None:
        """Bevestigd in het dashboard: laat de telefoon ophouden met herhalen."""
        key = str(event_id)
        receipt = self._receipts.get(key)
        if receipt is None:
            return
        watcher = self._watchers.pop(key, None)
        if watcher is not None:
            watcher.cancel()
        self._spawn(self._cancel_receipt(key, receipt))

    # ── Ontvangstbewijzen volgen ─────────────────────────────────────────────

    def _watch(self, key: str, receipt: str) -> None:
        if key in self._watchers:
            return
        task = asyncio.create_task(self._watch_receipt(key, receipt), name=f"pushover-{key}")
        self._watchers[key] = task

        def _done(finished: asyncio.Task[None]) -> None:
            if self._watchers.get(key) is finished:
                self._watchers.pop(key, None)

        task.add_done_callback(_done)

    async def _watch_receipt(self, key: str, receipt: str) -> None:
        try:
            while True:
                await asyncio.sleep(self._settings.poll_interval_seconds)

                if key != _SELFTEST_KEY and await self._acknowledged_in_db(int(key)):
                    # Via het dashboard bevestigd; de telefoon mag stil.
                    await self._cancel_receipt(key, receipt)
                    return

                try:
                    data = await self._get(f"/receipts/{receipt}.json")
                except PushoverError as exc:
                    _LOGGER.warning("Ontvangstbewijs %s niet op te halen: %s", receipt, exc)
                    continue

                if data.get("acknowledged") == 1:
                    by = str(data.get("acknowledged_by_device") or "telefoon")
                    await self._forget(key)
                    await self._handle_acknowledged(key, by)
                    return
                if data.get("expired") == 1:
                    await self._forget(key)
                    await self._handle_expired(key)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Volgen van Pushover-ontvangstbewijs %s liep vast", receipt)

    async def _handle_acknowledged(self, key: str, by: str) -> None:
        if key == _SELFTEST_KEY:
            _LOGGER.info("Testmelding bevestigd in de Pushover-app op %s", by)
            if self.on_selftest_acknowledged is not None:
                await self.on_selftest_acknowledged(f"pushover ({by})")
            return
        event_id = int(key)
        _LOGGER.warning("Alarm %d bevestigd in de Pushover-app op %s", event_id, by)
        await self._db.acknowledge(event_id, f"pushover ({by})")
        await self._db.log_notification(event_id, self.name, "acknowledged", f"bevestigd op {by}")
        if self._on_acknowledged is not None:
            self._on_acknowledged(event_id)

    async def _handle_expired(self, key: str) -> None:
        if key == _SELFTEST_KEY:
            _LOGGER.error(
                "Testmelding is niet bevestigd binnen %d seconden; controleer de "
                "Pushover-app en de instellingen van je toestel",
                _SELFTEST_EXPIRE_SECONDS,
            )
            return
        event_id = int(key)
        _LOGGER.error(
            "Alarm %d is na %d seconden herhalen op de telefoon nog steeds niet bevestigd; "
            "er gaat een nieuwe noodmelding uit",
            event_id,
            self._settings.expire_seconds,
        )
        await self._db.log_notification(
            event_id, self.name, "expired", "niet bevestigd op de telefoon; opnieuw gestuurd"
        )
        self._spawn(self._alarm_again(event_id))

    async def _alarm_again(self, event_id: int) -> None:
        """Blijf een onbevestigd alarm opnieuw als noodmelding sturen.

        Elke geslaagde verzending start een nieuwe watcher, die bij verlopen
        weer hier uitkomt. Zo blijft het alarm doorgaan tot iemand bevestigt,
        ook als Pushover tussendoor even niet bereikbaar is.
        """
        while True:
            row = await self._db.get_event(event_id)
            if row is None or row.acknowledged_at is not None:
                return
            if str(event_id) in self._watchers:
                return
            attempt = 1 + sum(
                1 for c in row.calls if c.variants == "pushover" and c.status == "sent"
            )
            try:
                await self._send(AlarmEvent.from_row(row), attempt=attempt)
            except NotifyError as exc:
                _LOGGER.error(
                    "Nieuwe noodmelding voor alarm %d mislukte (%s); over %ds opnieuw",
                    event_id,
                    exc,
                    _RESEND_RETRY_SECONDS,
                )
                await asyncio.sleep(_RESEND_RETRY_SECONDS)
                continue
            return

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _acknowledged_in_db(self, event_id: int) -> bool:
        row = await self._db.get_event(event_id)
        return row is not None and row.acknowledged_at is not None

    async def _cancel_receipt(self, key: str, receipt: str) -> None:
        await self._forget(key)
        try:
            await self._post(f"/receipts/{receipt}/cancel.json", {})
        except PushoverError as exc:
            _LOGGER.warning("Noodmelding %s intrekken bij Pushover mislukte: %s", receipt, exc)
        else:
            _LOGGER.info("Noodmelding voor %s ingetrokken na bevestiging elders", key)

    async def _remember(self, key: str, receipt: str) -> None:
        self._receipts[key] = receipt
        await self._db.set_state(_STATE_KEY, self._receipts)

    async def _forget(self, key: str) -> None:
        if self._receipts.pop(key, None) is not None:
            await self._db.set_state(_STATE_KEY, self._receipts)

    # ── HTTP ─────────────────────────────────────────────────────────────────

    async def _post(self, path: str, payload: dict[str, Any], *, attempts: int = 1) -> dict:
        assert self._client is not None, "start() is nog niet aangeroepen"
        body = {
            "token": self._settings.token or "",
            "user": self._settings.user_key or "",
            **payload,
        }
        last: PushoverError = PushoverError("geen poging gedaan")
        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.post(path, data=body)
                data = _json(response)
                if response.status_code == 200 and data.get("status") == 1:
                    return data
                errors = "; ".join(str(e) for e in data.get("errors") or [])
                last = PushoverError(errors or f"HTTP {response.status_code}")
                if 400 <= response.status_code < 500:
                    # Verkeerde sleutel of ongeldig verzoek: opnieuw proberen
                    # levert hetzelfde antwoord op.
                    break
            except httpx.HTTPError as exc:
                last = PushoverError(f"netwerkfout: {exc}")
            if attempt < attempts:
                await asyncio.sleep(5)
        raise last

    async def _get(self, path: str) -> dict:
        assert self._client is not None, "start() is nog niet aangeroepen"
        try:
            response = await self._client.get(path, params={"token": self._settings.token or ""})
        except httpx.HTTPError as exc:
            raise PushoverError(f"netwerkfout: {exc}") from exc
        data = _json(response)
        if response.status_code != 200 or data.get("status") != 1:
            errors = "; ".join(str(e) for e in data.get("errors") or [])
            raise PushoverError(errors or f"HTTP {response.status_code}")
        return data


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
