from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    database_path: Path = Path("data/team_energy.duckdb")
    poll_interval_seconds: float = 30.0
    stale_after_seconds: float = 120.0
    gap_threshold_seconds: float = 120.0
    request_timeout_seconds: float = 30.0
    web_username: str | None = None
    web_password: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: int | None = None
    telegram_report_interval_hours: float = 5.0
    telegram_timezone: str = "Asia/Yerevan"
    # Evan integration is fully opt-in: leave EVAN_PHONE/EVAN_PASSWORD unset
    # to run exactly as before, with no Evan polling at all.
    evan_phone: str | None = None
    evan_password: str | None = None
    evan_polls_path: Path = Path("data/evan_polls.jsonl")
    evan_grid_centers: tuple[tuple[float, float], ...] = (
        # A city/region-spanning grid, not just one tile - the endpoint only
        # returns stations inside the box you ask for (confirmed: one
        # Yerevan-center box returned just 14 of what's presumably a larger
        # national network). Coordinates below cover Yerevan in several
        # overlapping tiles plus the regional towns Team Energy's data
        # showed real charging activity in (Sevan, Vayk, Gyumri, Goris,
        # Dilijan, Vanadzor, Jermuk, Kapan). Add/adjust once you see real
        # coverage - each tile call costs one extra API request per poll.
        (40.1776, 44.5126),   # Yerevan center
        (40.20, 44.52),       # Yerevan north
        (40.15, 44.48),       # Yerevan south
        (40.19, 44.47),       # Yerevan west (Tairovi/Kentron area)
        (40.34, 44.94),       # Sevan
        (39.71, 45.40),       # Vayk / Vayots Dzor
        (40.79, 43.85),       # Gyumri
        (39.51, 46.34),       # Goris
        (40.74, 44.86),       # Dilijan
        (40.81, 44.49),       # Vanadzor
        (39.84, 45.68),       # Jermuk
        (39.21, 46.41),       # Kapan
    )

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv()
        raw_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        raw_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        raw_web_username = os.getenv("TEAM_ENERGY_WEB_USERNAME", "").strip()
        raw_web_password = os.getenv("TEAM_ENERGY_WEB_PASSWORD", "").strip()
        settings = cls(
            database_path=Path(
                os.getenv("TEAM_ENERGY_DB_PATH", "data/team_energy.duckdb")
            ),
            poll_interval_seconds=float(
                os.getenv("TEAM_ENERGY_POLL_INTERVAL_SECONDS", "30")
            ),
            stale_after_seconds=float(
                os.getenv("TEAM_ENERGY_STALE_AFTER_SECONDS", "120")
            ),
            gap_threshold_seconds=float(
                os.getenv("TEAM_ENERGY_GAP_THRESHOLD_SECONDS", "120")
            ),
            request_timeout_seconds=float(
                os.getenv("TEAM_ENERGY_REQUEST_TIMEOUT_SECONDS", "30")
            ),
            web_username=raw_web_username or None,
            web_password=raw_web_password or None,
            telegram_bot_token=raw_bot_token or None,
            telegram_chat_id=int(raw_chat_id) if raw_chat_id else None,
            telegram_report_interval_hours=float(
                os.getenv("TELEGRAM_REPORT_INTERVAL_HOURS", "5")
            ),
            telegram_timezone=os.getenv(
                "TELEGRAM_TIMEZONE", "Asia/Yerevan"
            ).strip(),
            evan_phone=os.getenv("EVAN_PHONE", "").strip() or None,
            evan_password=os.getenv("EVAN_PASSWORD", "").strip() or None,
            evan_polls_path=Path(
                os.getenv("EVAN_POLLS_PATH", "data/evan_polls.jsonl")
            ),
        )
        raw_grid = os.getenv("EVAN_GRID_CENTERS", "").strip()
        if raw_grid:
            # Format: "lat,lng;lat,lng;..." - overrides the built-in default
            # grid without needing a code change/redeploy.
            centers = []
            for pair in raw_grid.split(";"):
                pair = pair.strip()
                if not pair:
                    continue
                lat_str, lng_str = pair.split(",")
                centers.append((float(lat_str), float(lng_str)))
            settings = cls(**{**settings.__dict__, "evan_grid_centers": tuple(centers)})
        if settings.poll_interval_seconds < 1:
            raise ValueError("TEAM_ENERGY_POLL_INTERVAL_SECONDS must be at least 1")
        if settings.stale_after_seconds <= 0:
            raise ValueError("TEAM_ENERGY_STALE_AFTER_SECONDS must be positive")
        if settings.gap_threshold_seconds <= settings.poll_interval_seconds:
            raise ValueError(
                "TEAM_ENERGY_GAP_THRESHOLD_SECONDS must exceed the poll interval"
            )
        if settings.request_timeout_seconds <= 0:
            raise ValueError("TEAM_ENERGY_REQUEST_TIMEOUT_SECONDS must be positive")
        if bool(settings.web_username) != bool(settings.web_password):
            raise ValueError(
                "TEAM_ENERGY_WEB_USERNAME and TEAM_ENERGY_WEB_PASSWORD "
                "must either both be set or both be empty"
            )
        if settings.telegram_report_interval_hours <= 0:
            raise ValueError("TELEGRAM_REPORT_INTERVAL_HOURS must be positive")
        if not settings.telegram_timezone:
            raise ValueError("TELEGRAM_TIMEZONE must not be empty")
        return settings
