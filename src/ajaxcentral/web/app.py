"""FastAPI-app: REST, WebSocket en het dashboard zelf."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from ..bus import EventBus
from ..config import Config
from ..db import Database
from ..models import AlarmEvent, utcnow
from ..normalize import TEST_ALARM_KINDS, simulated_alarm
from ..selftest import SelfTest
from ..state import SystemState

_LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "ajaxcentral_session"
SESSION_MAX_AGE = int(timedelta(days=30).total_seconds())


#: Wat voor melder bij welk soort testalarm hoort, voor foutmeldingen.
_KIND_DEVICE_LABELS = {"fire": "rookmelder", "burglary": "inbraakmelder"}


class TestAlarmRequest(BaseModel):
    """Welk alarm je wilt nabootsen, en van welke melder."""

    kind: str
    device_id: str | None = None


class LoginRequest(BaseModel):
    """JSON in plaats van formulierdata: dat scheelt de python-multipart
    afhankelijkheid, en de client is toch al JavaScript."""

    username: str
    password: str


@dataclass
class WebContext:
    """Alles waar de webapp bij moet. Eén object scheelt tien globals."""

    config: Config
    db: Database
    bus: EventBus[AlarmEvent]
    state: SystemState
    selftest: SelfTest | None = None
    receiver: Any = None
    matrix: Any = None
    on_acknowledge: Any = None
    #: Levert een zelfgemaakt event in bij de pijplijn (pipeline.submit).
    submit: Any = None


def _channels(config: Config) -> list[str]:
    """Namen van de meldkanalen die aan staan, voor het dashboard."""
    return [
        name
        for name, enabled in (
            ("pushover", config.pushover.enabled),
            ("matrix", config.matrix.enabled),
        )
        if enabled
    ]


def create_app(context: WebContext) -> FastAPI:
    config = context.config
    secret = config.web.secret
    if not secret:
        # Geen vaste terugvalsleutel: die staat in de publieke code, en daarmee
        # kan iedereen een geldige sessie-cookie ondertekenen en het wachtwoord
        # omzeilen. Een willekeurige sleutel per start kost alleen dat je na
        # een herstart opnieuw moet inloggen.
        secret = secrets.token_hex(32)
        _LOGGER.warning(
            "AJAXCENTRAL_WEB_SECRET ontbreekt; tijdelijke sessiesleutel aangemaakt. "
            "Sessies vervallen bij elke herstart — zet de sleutel in .env."
        )
    serializer = URLSafeTimedSerializer(secret, salt="ajaxcentral-session")

    app = FastAPI(title="Ajax Alarmcentrale", docs_url=None, redoc_url=None)
    app.state.context = context

    # ── Authenticatie ────────────────────────────────────────────────────────

    def current_user(request: Request) -> str:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise HTTPException(status_code=401, detail="Niet ingelogd")
        try:
            return str(serializer.loads(token, max_age=SESSION_MAX_AGE))
        except BadSignature as exc:
            raise HTTPException(status_code=401, detail="Sessie ongeldig") from exc

    def user_from_websocket(websocket: WebSocket) -> str | None:
        token = websocket.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        try:
            return str(serializer.loads(token, max_age=SESSION_MAX_AGE))
        except BadSignature:
            return None

    @app.post("/api/login")
    async def login(credentials: LoginRequest) -> Response:
        from .auth import verify_password

        username, password = credentials.username, credentials.password

        expected_hash = config.web.password_hash
        if not expected_hash:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Er is geen wachtwoord ingesteld. Draai "
                    "'python -m ajaxcentral.web.auth hash <wachtwoord>' en zet de "
                    "uitkomst in .env."
                ),
            )
        if username != config.web.username or not verify_password(password, expected_hash):
            # Bewust geen onderscheid tussen 'gebruiker onbekend' en 'wachtwoord
            # fout': dat verklapt of een gebruikersnaam bestaat.
            _LOGGER.warning("Mislukte inlogpoging voor gebruiker %r", username)
            raise HTTPException(status_code=401, detail="Onjuiste gebruikersnaam of wachtwoord")

        response = JSONResponse({"ok": True, "user": username})
        response.set_cookie(
            SESSION_COOKIE,
            serializer.dumps(username),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.post("/api/logout")
    async def logout() -> Response:
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get("/api/session")
    async def session(request: Request) -> dict[str, Any]:
        try:
            user = current_user(request)
        except HTTPException:
            return {"authenticated": False, "password_set": bool(config.web.password_hash)}
        return {"authenticated": True, "user": user}

    # ── Status en events ─────────────────────────────────────────────────────

    @app.get("/api/status")
    async def status(user: str = Depends(current_user)) -> dict[str, Any]:
        data = context.state.to_dict()
        data["open_alarms"] = len(await context.db.unacknowledged_alarms())
        context.state.open_alarms = data["open_alarms"]
        data["failed_notifications_24h"] = await context.db.recent_failures(24)
        data["watchdog_threshold_seconds"] = config.sia.offline_after_seconds
        data["selftest"] = await context.selftest.status() if context.selftest is not None else None
        data["matrix_enabled"] = config.matrix.enabled
        data["pushover_enabled"] = config.pushover.enabled
        data["channels"] = _channels(config)
        data["now"] = utcnow().isoformat()
        return data

    def _moment(value: str | None, name: str) -> datetime | None:
        """ISO-tijdstip uit een queryparameter; zonder tijdzone geldt UTC."""
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"{name} is geen geldig tijdstip") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    async def _filtered_events(
        *,
        limit: int,
        offset: int,
        severity: str | None,
        category: str | None,
        device_id: str | None,
        partition_id: str | None,
        include_heartbeat: bool,
        since: str | None,
        until: str | None,
    ) -> list[Any]:
        # Bij een ping-interval van een minuut levert de hub ruim 1400
        # hartslagen per dag. Standaard blijven die buiten het logboek, anders
        # verdrinkt alles wat je wél wilt zien erin.
        rows = await context.db.list_events(
            limit=limit,
            offset=offset,
            severity=severity,
            exclude_severities=None if include_heartbeat else ["heartbeat"],
            category=category,
            device_id=device_id,
            partition_id=partition_id,
            since=_moment(since, "since"),
            until=_moment(until, "until"),
        )
        return list(rows)

    @app.get("/api/events")
    async def events(
        user: str = Depends(current_user),
        limit: int = 100,
        offset: int = 0,
        severity: str | None = None,
        category: str | None = None,
        device_id: str | None = None,
        partition_id: str | None = None,
        include_heartbeat: bool = False,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        rows = await _filtered_events(
            limit=min(limit, 500),
            offset=offset,
            severity=severity,
            category=category,
            device_id=device_id,
            partition_id=partition_id,
            include_heartbeat=include_heartbeat,
            since=since,
            until=until,
        )
        return {"events": [row.to_dict(include_children=True) for row in rows]}

    @app.get("/api/events.csv")
    async def events_csv(
        user: str = Depends(current_user),
        severity: str | None = None,
        category: str | None = None,
        device_id: str | None = None,
        partition_id: str | None = None,
        include_heartbeat: bool = False,
        since: str | None = None,
        until: str | None = None,
    ) -> Response:
        """Het logboek als CSV, voor politie, verzekeraar of je eigen archief.

        Tijden staan in de tijdzone uit config.yaml, want een aangifte met
        UTC-tijden roept alleen maar vragen op. Puntkomma als scheidingsteken:
        dat opent Excel in Nederland zonder importwizard.
        """
        rows = await _filtered_events(
            limit=10000,
            offset=0,
            severity=severity,
            category=category,
            device_id=device_id,
            partition_id=partition_id,
            include_heartbeat=include_heartbeat,
            since=since,
            until=until,
        )
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
        writer.writerow(
            [
                "tijdstip",
                "ontvangen",
                "ernst",
                "categorie",
                "code",
                "titel",
                "apparaat",
                "apparaat_id",
                "groep",
                "gebruiker",
                "bericht",
                "bron",
                "bevestigd_op",
                "bevestigd_door",
            ]
        )

        def local(moment: datetime | None) -> str:
            return config.to_local(moment).strftime("%Y-%m-%d %H:%M:%S") if moment else ""

        for row in rows:
            writer.writerow(
                [
                    local(row.event_at),
                    local(row.received_at),
                    row.severity,
                    row.category,
                    row.code,
                    row.title,
                    row.device_name,
                    row.device_id or "",
                    row.partition_name,
                    row.user_name or "",
                    row.message or "",
                    row.source,
                    local(row.acknowledged_at),
                    row.acknowledged_by or "",
                ]
            )
        stamp = config.to_local(utcnow()).strftime("%Y%m%d-%H%M")
        return PlainTextResponse(
            "\ufeff" + buffer.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="logboek-{stamp}.csv"'},
        )

    @app.get("/api/alarms")
    async def alarms(user: str = Depends(current_user)) -> dict[str, Any]:
        rows = await context.db.unacknowledged_alarms()
        return {"alarms": [row.to_dict(include_children=True) for row in rows]}

    @app.post("/api/events/{event_id}/acknowledge")
    async def acknowledge(event_id: int, user: str = Depends(current_user)) -> dict[str, Any]:
        row = await context.db.acknowledge(event_id, user)
        if row is None:
            raise HTTPException(status_code=404, detail="Onbekend event")
        if context.on_acknowledge is not None:
            context.on_acknowledge(event_id)
        context.state.open_alarms = len(await context.db.unacknowledged_alarms())
        return {"ok": True, "event": row.to_dict(include_children=True)}

    @app.post("/api/alarms/acknowledge-all")
    async def acknowledge_all(user: str = Depends(current_user)) -> dict[str, Any]:
        open_rows = await context.db.unacknowledged_alarms()
        ids = [row.id for row in open_rows]
        count = await context.db.acknowledge_all_alarms(user)
        if context.on_acknowledge is not None:
            for event_id in ids:
                context.on_acknowledge(event_id)
        context.state.open_alarms = 0
        return {"ok": True, "acknowledged": count}

    # ── Testoproep ───────────────────────────────────────────────────────────

    @app.post("/api/selftest/ring")
    async def selftest_ring(user: str = Depends(current_user)) -> dict[str, Any]:
        if context.selftest is None:
            raise HTTPException(status_code=503, detail="Geen meldkanaal aan")
        run = await context.selftest.run_once(kind="manual")
        return {"ok": run.ring_status == "sent", "run": run.to_dict()}

    @app.post("/api/selftest/acknowledge")
    async def selftest_acknowledge(user: str = Depends(current_user)) -> dict[str, Any]:
        if context.selftest is None:
            raise HTTPException(status_code=503, detail="Zelftest staat uit")
        run = await context.selftest.acknowledge_latest(user)
        if run is None:
            raise HTTPException(status_code=404, detail="Nog geen testoproep uitgevoerd")
        return {"ok": True, "run": run.to_dict()}

    # ── Testalarm ────────────────────────────────────────────────────────────
    #
    # De testmelding hierboven bewijst dat Pushover je telefoon bereikt. Dit
    # bewijst de rest: dat een brand- of inbraakalarm de hele keten doorloopt —
    # pijplijn, logboek, noodmelding, open alarm op het dashboard, bevestiging
    # over en weer. Het is een echt alarm met een echte code; alleen de titel,
    # de bron en het bericht verraden dat het een test is.

    @app.post("/api/selftest/alarm")
    async def selftest_alarm(
        body: TestAlarmRequest, user: str = Depends(current_user)
    ) -> dict[str, Any]:
        if context.selftest is None or context.submit is None:
            raise HTTPException(status_code=503, detail="Geen meldkanaal aan")
        if body.kind not in TEST_ALARM_KINDS:
            raise HTTPException(
                status_code=400,
                detail=f"Onbekend soort testalarm; kies uit {', '.join(TEST_ALARM_KINDS)}",
            )
        device_id = (body.device_id or "").strip() or None
        if device_id is not None and device_id not in config.devices:
            raise HTTPException(status_code=400, detail="Onbekend apparaat")
        device_type = config.device_type(device_id)
        if device_type is not None and device_type != body.kind:
            # Een inbraakmelder die brand meldt bestaat niet; zo'n test bewijst
            # niets over de echte melder en zaait alleen verwarring in het logboek.
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{config.device_name(device_id)} is geen "
                    f"{_KIND_DEVICE_LABELS.get(body.kind, body.kind)}; kies een ander apparaat"
                ),
            )
        alarm = simulated_alarm(body.kind, config, device_id=device_id, by=user)
        _LOGGER.warning("Testalarm gestart door %s: %s", user, alarm.summary())
        context.submit(alarm)
        return {"ok": True, "event": alarm.to_dict()}

    @app.get("/api/selftest/alarms")
    async def selftest_alarms(user: str = Depends(current_user)) -> dict[str, Any]:
        rows = await context.db.list_events(limit=5, source="test")
        return {
            "alarms": [row.to_dict(include_children=True) for row in rows],
            "kinds": list(TEST_ALARM_KINDS),
            "devices": [
                {"id": key, "name": name, "type": config.device_type(key)}
                for key, name in config.devices.items()
            ],
        }

    # ── Diagnostiek ──────────────────────────────────────────────────────────

    @app.get("/api/diagnostics")
    async def diagnostics(user: str = Depends(current_user)) -> dict[str, Any]:
        receiver = context.receiver
        return {
            "raw_frames": list(getattr(receiver, "raw_log", [])),
            "counters": getattr(receiver, "counts", {}),
            "sia": {
                "host": config.sia.host,
                "port": config.sia.port,
                "protocol": config.sia.protocol,
                "account_id": config.sia.account_id,
                "encrypted": bool(config.sia.key),
                "ping_interval_seconds": config.sia.ping_interval_seconds,
            },
            "channels": _channels(config),
            "ring_variants": (
                [v.name for v in context.matrix.ring_sender.selected_variants()]
                if context.matrix is not None
                else []
            ),
        }

    # ── Live feed ────────────────────────────────────────────────────────────

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        if user_from_websocket(websocket) is None:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            async with context.bus.subscribe() as queue:
                await websocket.send_json({"type": "status", "data": context.state.to_dict()})
                while True:
                    try:
                        alarm = await asyncio.wait_for(queue.get(), timeout=25)
                    except TimeoutError:
                        # Regelmatig een levensteken: zonder verkeer sluiten
                        # sommige proxies en telefoons de verbinding stilletjes.
                        await websocket.send_json({"type": "ping"})
                        continue
                    await websocket.send_json(
                        {
                            "type": "event",
                            "data": alarm.to_dict(),
                            "status": context.state.to_dict(),
                        }
                    )
        except WebSocketDisconnect:
            pass
        except Exception:  # pragma: no cover - verbinding weggevallen
            _LOGGER.debug("WebSocket beëindigd", exc_info=True)

    # ── Statische bestanden ──────────────────────────────────────────────────

    @app.get("/")
    async def index() -> Response:
        return FileResponse(STATIC_DIR / "index.html")

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def _no_stale_ui(request: Request, call_next: Any) -> Response:
        # Zonder Cache-Control mag een browser app.js dagenlang uit zijn cache
        # halen, en dan toont het dashboard na een update nog oude teksten.
        # "no-cache" dwingt hervalidatie af; dankzij de ETag kost dat niets.
        response: Response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
        if exc.status_code == 401 and not request.url.path.startswith("/api"):
            return RedirectResponse("/")
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app
