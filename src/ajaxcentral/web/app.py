"""FastAPI-app: REST, WebSocket en het dashboard zelf."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from ..bus import EventBus
from ..config import Config
from ..db import Database
from ..models import AlarmEvent, utcnow
from ..selftest import SelfTest
from ..state import SystemState

_LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "ajaxcentral_session"
SESSION_MAX_AGE = int(timedelta(days=30).total_seconds())


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


def create_app(context: WebContext) -> FastAPI:
    config = context.config
    serializer = URLSafeTimedSerializer(
        config.web.secret or "ajaxcentral-onveilig", salt="ajaxcentral-session"
    )

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
        data["now"] = utcnow().isoformat()
        return data

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
    ) -> dict[str, Any]:
        # Bij een ping-interval van een minuut levert de hub ruim 1400
        # hartslagen per dag. Standaard blijven die buiten het logboek, anders
        # verdrinkt alles wat je wél wilt zien erin.
        rows = await context.db.list_events(
            limit=min(limit, 500),
            offset=offset,
            severity=severity,
            exclude_severities=None if include_heartbeat else ["heartbeat"],
            category=category,
            device_id=device_id,
            partition_id=partition_id,
        )
        return {"events": [row.to_dict(include_children=True) for row in rows]}

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
        if context.selftest is None or context.matrix is None:
            raise HTTPException(status_code=503, detail="Matrix staat uit")
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

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
        if exc.status_code == 401 and not request.url.path.startswith("/api"):
            return RedirectResponse("/")
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app
