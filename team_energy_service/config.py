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
    request_timeout_seconds: float = 30.0
    telegram_bot_token: str | None = None
    telegram_chat_id: int | None = None
    telegram_report_interval_hours: float = 5.0
    telegram_timezone: str = "Asia/Yerevan"

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv()
        raw_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        raw_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
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
            request_timeout_seconds=float(
                os.getenv("TEAM_ENERGY_REQUEST_TIMEOUT_SECONDS", "30")
            ),
            telegram_bot_token=raw_bot_token or None,
            telegram_chat_id=int(raw_chat_id) if raw_chat_id else None,
            telegram_report_interval_hours=float(
                os.getenv("TELEGRAM_REPORT_INTERVAL_HOURS", "5")
            ),
            telegram_timezone=os.getenv(
                "TELEGRAM_TIMEZONE", "Asia/Yerevan"
            ).strip(),
        )
        if settings.poll_interval_seconds < 1:
            raise ValueError("TEAM_ENERGY_POLL_INTERVAL_SECONDS must be at least 1")
        if settings.stale_after_seconds <= 0:
            raise ValueError("TEAM_ENERGY_STALE_AFTER_SECONDS must be positive")
        if settings.request_timeout_seconds <= 0:
            raise ValueError("TEAM_ENERGY_REQUEST_TIMEOUT_SECONDS must be positive")
        if settings.telegram_report_interval_hours <= 0:
            raise ValueError("TELEGRAM_REPORT_INTERVAL_HOURS must be positive")
        if not settings.telegram_timezone:
            raise ValueError("TELEGRAM_TIMEZONE must not be empty")
        return settings
