from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .database import Database
from .poller import Poller
from .provider import TeamEnergyClient
from .telegram import TelegramService


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
STATIC_DIRECTORY = Path(__file__).resolve().parent / "static"


def create_app(
    settings: Settings | None = None,
    provider: TeamEnergyClient | None = None,
    start_poller: bool = True,
) -> FastAPI:
    settings = settings or Settings.from_environment()
    database = Database(settings.database_path, settings.gap_threshold_seconds)
    provider = provider or TeamEnergyClient(settings.request_timeout_seconds)
    poller = Poller(provider, database, settings.poll_interval_seconds)
    telegram = TelegramService(
        token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        report_interval_hours=settings.telegram_report_interval_hours,
        timezone_name=settings.telegram_timezone,
        database=database,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(database.initialize)
        app.state.database = database
        app.state.poller = poller
        app.state.telegram = telegram
        polling_task = (
            asyncio.create_task(poller.run(), name="team-energy-poller")
            if start_poller
            else None
        )
        telegram.start()
        try:
            yield
        finally:
            poller.stop()
            await telegram.stop()
            if polling_task:
                await polling_task
            await provider.close()

    app = FastAPI(
        title="Team Energy History API",
        version="0.1.0",
        description="Private cached station API with interval-compressed history.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def require_basic_auth(request: Request, call_next):
        if request.url.path == "/healthz" or not settings.web_username:
            return await call_next(request)

        authorization = request.headers.get("Authorization", "")
        scheme, _, encoded = authorization.partition(" ")
        supplied_username = ""
        supplied_password = ""
        if scheme.lower() == "basic" and encoded:
            try:
                decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
                supplied_username, separator, supplied_password = decoded.partition(":")
                if not separator:
                    supplied_username = ""
                    supplied_password = ""
            except (binascii.Error, UnicodeDecodeError):
                pass

        authenticated = secrets.compare_digest(
            supplied_username.encode("utf-8"),
            settings.web_username.encode("utf-8"),
        ) and secrets.compare_digest(
            supplied_password.encode("utf-8"),
            (settings.web_password or "").encode("utf-8"),
        )
        if not authenticated:
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
                headers={"WWW-Authenticate": 'Basic realm="Toki", charset="UTF-8"'},
            )
        return await call_next(request)

    app.mount(
        "/static",
        StaticFiles(directory=STATIC_DIRECTORY),
        name="static",
    )

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "service": "Team Energy History API",
            "docs": "/docs",
            "health": "/health",
            "dashboard": "/dashboard",
        }

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/dashboard", response_class=FileResponse)
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIRECTORY / "dashboard.html")

    @app.get("/health")
    async def health(request: Request) -> dict:
        state = await asyncio.to_thread(request.app.state.database.health)
        last_success = state.get("last_success_at")
        age_seconds = (
            (datetime.now(timezone.utc) - last_success).total_seconds()
            if last_success
            else None
        )
        stale = age_seconds is None or age_seconds > settings.stale_after_seconds
        state.update(
            {
                "status": "initializing"
                if last_success is None
                else ("degraded" if stale or state["last_error"] else "healthy"),
                "stale": stale,
                "data_age_seconds": round(age_seconds, 3)
                if age_seconds is not None
                else None,
                "poll_interval_seconds": settings.poll_interval_seconds,
            }
        )
        return state

    @app.get("/summary")
    async def summary(request: Request) -> dict:
        return await asyncio.to_thread(request.app.state.database.summary)

    @app.get("/analytics/map")
    async def map_analytics(
        request: Request,
        hours: Annotated[int, Query(ge=0, le=24 * 366)] = 24,
    ) -> dict:
        return await asyncio.to_thread(
            request.app.state.database.dashboard_analytics,
            hours or None,
        )

    @app.get("/telegram/status")
    async def telegram_status(request: Request) -> dict:
        return request.app.state.telegram.public_status()

    @app.get("/collector/gaps")
    async def collector_gaps(
        request: Request,
        hours: Annotated[int, Query(ge=1, le=24 * 366)] = 24 * 30,
    ) -> dict:
        gaps = await asyncio.to_thread(
            request.app.state.database.list_collector_gaps,
            hours,
        )
        return {"count": len(gaps), "gaps": gaps}

    @app.get("/stations")
    async def stations(
        request: Request,
        status: Annotated[
            str | None,
            Query(pattern="^(available|busy|maintenance|unknown)$"),
        ] = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict:
        rows = await asyncio.to_thread(
            request.app.state.database.list_stations,
            status,
            q,
            limit,
            offset,
        )
        return {"count": len(rows), "stations": rows}

    @app.get("/stations/{station_id}")
    async def station(station_id: str, request: Request) -> dict:
        result = await asyncio.to_thread(
            request.app.state.database.get_station,
            station_id,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="Station not found")
        return result

    @app.get("/stations/{station_id}/history")
    async def station_history(
        station_id: str,
        request: Request,
        hours: Annotated[int, Query(ge=1, le=24 * 366)] = 24,
    ) -> dict:
        result = await asyncio.to_thread(
            request.app.state.database.station_history,
            station_id,
            hours,
        )
        if not result and await asyncio.to_thread(
            request.app.state.database.get_station, station_id
        ) is None:
            raise HTTPException(status_code=404, detail="Station not found")
        return {"station_id": station_id, "history": result}

    @app.get("/connectors/{connector_id}/history")
    async def connector_history(
        connector_id: str,
        request: Request,
        hours: Annotated[int, Query(ge=1, le=24 * 366)] = 24,
    ) -> dict:
        result = await asyncio.to_thread(
            request.app.state.database.connector_history,
            connector_id,
            hours,
        )
        if not result:
            raise HTTPException(status_code=404, detail="Connector not found")
        return {"connector_id": connector_id, "history": result}

    return app


app = create_app()
