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
from .evan_poller import EvanPoller
from .evan_provider import EvanClient
from .evan_store import EvanStore
from .ecocars_poller import EcoCarsPoller
from .ecocars_provider import EcoCarsClient
from .ecocars_store import EcoCarsStore
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

    evan_poller: EvanPoller | None = None
    evan_store: EvanStore | None = None
    if settings.evan_phone and settings.evan_password:
        evan_store = EvanStore(settings.evan_polls_path)
        evan_client = EvanClient(
            phone=settings.evan_phone,
            password=settings.evan_password,
            request_timeout_seconds=settings.request_timeout_seconds,
        )
        evan_poller = EvanPoller(
            client=evan_client,
            store=evan_store,
            interval_seconds=settings.poll_interval_seconds,
            grid_centers=settings.evan_grid_centers,
        )

    ecocars_poller: EcoCarsPoller | None = None
    ecocars_store: EcoCarsStore | None = None
    if settings.ecocars_enabled:
        ecocars_store = EcoCarsStore(settings.ecocars_polls_path)
        ecocars_client = EcoCarsClient(
            request_timeout_seconds=settings.request_timeout_seconds,
        )
        ecocars_poller = EcoCarsPoller(
            client=ecocars_client,
            store=ecocars_store,
            interval_seconds=settings.poll_interval_seconds,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(database.initialize)
        app.state.database = database
        app.state.poller = poller
        app.state.telegram = telegram
        app.state.evan_store = evan_store
        app.state.ecocars_store = ecocars_store
        polling_task = (
            asyncio.create_task(poller.run(), name="team-energy-poller")
            if start_poller
            else None
        )
        evan_task = None
        if evan_poller is not None and evan_store is not None:
            await asyncio.to_thread(evan_store.initialize)
            if start_poller:
                evan_task = asyncio.create_task(evan_poller.run(), name="evan-poller")
        ecocars_task = None
        if ecocars_poller is not None and ecocars_store is not None:
            await asyncio.to_thread(ecocars_store.initialize)
            if start_poller:
                ecocars_task = asyncio.create_task(
                    ecocars_poller.run(), name="ecocars-poller"
                )
        telegram.start()
        try:
            yield
        finally:
            poller.stop()
            await telegram.stop()
            if polling_task:
                await polling_task
            if evan_poller is not None:
                evan_poller.stop()
            if evan_task:
                await evan_task
            if ecocars_poller is not None:
                ecocars_poller.stop()
            if ecocars_task:
                await ecocars_task
            await provider.close()
            if evan_poller is not None:
                await evan_poller.client.close()
            if ecocars_poller is not None:
                await ecocars_poller.client.close()

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

    @app.get("/debug/recent-summary")
    async def debug_recent_summary(
        request: Request,
        hours: Annotated[int, Query(ge=1, le=24 * 60)] = 24,
    ) -> dict:
        """Lightweight, time-scoped Team Energy summary (cars served, hours,
        AC/DC split with kWh ceilings, revenue at 0.5 and 0.3 load factors,
        battery in/out/delta, top 3 stations by hours and by cars). Meant as
        a cheap daily check that doesn't require pulling the full /excel
        export. Requires the shared web credentials.
        """
        database = request.app.state.database
        return await asyncio.to_thread(database.recent_summary, hours)

    @app.get("/debug/daily-summary")
    async def debug_daily_summary(
        request: Request,
        start_date: str,
        end_date: str,
    ) -> dict:
        """Same metrics as /debug/recent-summary, but returned as one block
        per calendar day between start_date and end_date (inclusive), e.g.
        start_date=2026-08-19&end_date=2026-09-04. Dates are "YYYY-MM-DD".
        Works on any historical range already collected - Team Energy's
        database has been persistent since the start, so this should cover
        the full history. Requires the shared web credentials.
        """
        database = request.app.state.database
        return await asyncio.to_thread(
            database.daily_summary, start_date, end_date
        )

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

    @app.get("/debug/raw-sample")
    async def debug_raw_sample(request: Request) -> dict:
        """Return one raw station+connector exactly as the Team Energy API sends
        it, so every available field (including any metered energy/kWh not
        currently parsed) can be inspected. Requires the shared web credentials.
        """
        provider = request.app.state.poller.provider
        sample = getattr(provider, "last_raw_sample", None)
        if sample is None:
            return {
                "available": False,
                "detail": "No raw sample captured yet; wait for the next poll.",
            }
        return {"available": True, "raw_station": sample}

    @app.get("/debug/evan-summary")
    async def debug_evan_summary(
        request: Request,
        hours: Annotated[int, Query(ge=1, le=24 * 60)] = 24,
    ) -> dict:
        """On-demand usage summary from raw Evan polls, e.g. 'how many hours
        was each connector charging in the last 24h'. Requires the shared
        web credentials. Returns a note if Evan polling isn't configured.
        """
        store = getattr(request.app.state, "evan_store", None)
        if store is None:
            return {
                "available": False,
                "detail": (
                    "Evan polling isn't configured (EVAN_PHONE/EVAN_PASSWORD "
                    "not set)."
                ),
            }
        summary = await asyncio.to_thread(store.summary_last_hours, hours)
        return {"available": True, **summary}

    @app.get("/debug/evan-sessions")
    async def debug_evan_sessions(
        request: Request,
        hours: Annotated[int, Query(ge=1, le=24 * 60)] = 24,
        include_sessions: Annotated[bool, Query()] = False,
    ) -> dict:
        """Real charging sessions reconstructed from the Evan poll log
        (session start/end detected via status transitions, same idea as
        Team Energy's interval tracking), giving an actual cars-served
        count instead of an hours-based estimate. By default returns a
        lean summary (totals, AC/DC split with kWh, revenue, battery,
        top 3 stations) without the full per-session list - pass
        include_sessions=true for the raw list. Requires the shared web
        credentials.
        """
        store = getattr(request.app.state, "evan_store", None)
        if store is None:
            return {
                "available": False,
                "detail": (
                    "Evan polling isn't configured (EVAN_PHONE/EVAN_PASSWORD "
                    "not set)."
                ),
            }
        sessions = await asyncio.to_thread(
            store.sessions_last_hours, hours, include_sessions
        )
        return {"available": True, **sessions}

    @app.get("/debug/evan-daily")
    async def debug_evan_daily(
        request: Request,
        start_date: str,
        end_date: str,
    ) -> dict:
        """Same metrics as /debug/evan-sessions, but returned as one block
        per calendar day between start_date and end_date (inclusive), e.g.
        start_date=2026-08-19&end_date=2026-09-04. Dates are "YYYY-MM-DD".
        Only covers whatever history is actually in the persistent poll
        log - if EVAN_POLLS_PATH was only recently pointed at the
        persistent volume, earlier days in the range may show zero/partial
        data. Requires the shared web credentials.
        """
        store = getattr(request.app.state, "evan_store", None)
        if store is None:
            return {
                "available": False,
                "detail": (
                    "Evan polling isn't configured (EVAN_PHONE/EVAN_PASSWORD "
                    "not set)."
                ),
            }
        result = await asyncio.to_thread(
            store.daily_sessions_summary, start_date, end_date
        )
        return {"available": True, **result}

    @app.get("/debug/ecocars-sessions")
    async def debug_ecocars_sessions(
        request: Request,
        hours: Annotated[int, Query(ge=1, le=24 * 60)] = 24,
        include_sessions: Annotated[bool, Query()] = False,
    ) -> dict:
        """Real charging sessions reconstructed from the EcoCars poll log,
        same session-transition detection as Team Energy and Evan. By
        default returns a lean summary without the full per-session list -
        pass include_sessions=true for the raw list. Requires the shared
        web credentials.
        """
        store = getattr(request.app.state, "ecocars_store", None)
        if store is None:
            return {
                "available": False,
                "detail": "EcoCars polling isn't configured (ECOCARS_ENABLED not set).",
            }
        sessions = await asyncio.to_thread(
            store.sessions_last_hours, hours, include_sessions
        )
        return {"available": True, **sessions}

    @app.get("/debug/ecocars-daily")
    async def debug_ecocars_daily(
        request: Request,
        start_date: str,
        end_date: str,
    ) -> dict:
        """Same metrics as /debug/ecocars-sessions, but returned as one
        block per calendar day between start_date and end_date (inclusive),
        e.g. start_date=2026-08-19&end_date=2026-09-04. Dates are
        "YYYY-MM-DD". Only covers whatever history is actually in the
        persistent poll log - if ECOCARS_POLLS_PATH was only recently
        pointed at the persistent volume, earlier days in the range may
        show zero/partial data, and Georgian stations remain mixed in until
        the Armenia-only filter is deployed. Requires the shared web
        credentials.
        """
        store = getattr(request.app.state, "ecocars_store", None)
        if store is None:
            return {
                "available": False,
                "detail": "EcoCars polling isn't configured (ECOCARS_ENABLED not set).",
            }
        result = await asyncio.to_thread(
            store.daily_sessions_summary, start_date, end_date
        )
        return {"available": True, **result}

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