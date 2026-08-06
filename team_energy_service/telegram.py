from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .database import Database


logger = logging.getLogger(__name__)
TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_DOCUMENT_LIMIT = 50 * 1024 * 1024


class TelegramError(RuntimeError):
    """Raised when Telegram rejects or cannot receive a bot request."""


class TelegramClient:
    def __init__(
        self,
        token: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url="https://api.telegram.org",
            timeout=httpx.Timeout(30.0, read=35.0),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        try:
            response = await self._client.post(
                f"/bot{self._token}/{method}",
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise TelegramError(f"Telegram {method} request failed") from exc
        if response.status_code != 200:
            raise TelegramError(f"Telegram {method} returned HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError as exc:
            raise TelegramError(f"Telegram {method} returned invalid JSON") from exc
        if result.get("ok") is not True:
            description = result.get("description") or "unknown Telegram error"
            raise TelegramError(f"Telegram {method} failed: {description}")
        return result.get("result")

    async def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        if len(text) > TELEGRAM_MESSAGE_LIMIT:
            raise TelegramError("Telegram message exceeds 4096 characters")
        result = await self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
        if not isinstance(result, dict):
            raise TelegramError("Telegram sendMessage returned an unexpected result")
        return result

    async def send_document(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
        caption: str | None = None,
    ) -> dict[str, Any]:
        if len(content) > TELEGRAM_DOCUMENT_LIMIT:
            raise TelegramError("Telegram document exceeds the 50 MB upload limit")
        try:
            response = await self._client.post(
                f"/bot{self._token}/sendDocument",
                data={
                    "chat_id": str(chat_id),
                    "caption": caption or "",
                },
                files={
                    "document": (
                        filename,
                        content,
                        "application/zip",
                    )
                },
            )
        except httpx.HTTPError as exc:
            raise TelegramError("Telegram sendDocument request failed") from exc
        if response.status_code != 200:
            raise TelegramError(
                f"Telegram sendDocument returned HTTP {response.status_code}"
            )
        try:
            result = response.json()
        except ValueError as exc:
            raise TelegramError("Telegram sendDocument returned invalid JSON") from exc
        if result.get("ok") is not True or not isinstance(result.get("result"), dict):
            description = result.get("description") or "unknown Telegram error"
            raise TelegramError(f"Telegram sendDocument failed: {description}")
        return result["result"]

    async def get_updates(
        self,
        offset: int | None,
        timeout_seconds: int = 20,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout_seconds,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self._call("getUpdates", payload)
        if not isinstance(result, list):
            raise TelegramError("Telegram getUpdates returned an unexpected result")
        return [update for update in result if isinstance(update, dict)]


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown Telegram report timezone: {name}") from exc


def format_report(
    summary: dict[str, Any],
    busy_stations: list[dict[str, Any]],
    timezone_name: str,
) -> str:
    observed_at = summary.get("observed_at")
    if isinstance(observed_at, datetime):
        observed = observed_at.astimezone(_timezone(timezone_name))
        updated_text = observed.strftime("%Y-%m-%d %H:%M:%S %Z")
        age_seconds = max(
            0,
            int((datetime.now(timezone.utc) - observed_at).total_seconds()),
        )
    else:
        updated_text = "not available"
        age_seconds = 0

    stations = summary.get("stations") or {}
    connectors = summary.get("connectors") or {}
    station_total = sum(int(value) for value in stations.values())
    connector_total = sum(int(value) for value in connectors.values())
    lines = [
        "⚡ Team Energy report",
        f"Updated: {updated_text} ({age_seconds}s ago)",
        "",
        f"Stations: {station_total}",
        f"🟢 Available: {stations.get('available', 0)}",
        f"🔴 Busy: {stations.get('busy', 0)}",
        f"🛠 Maintenance: {stations.get('maintenance', 0)}",
        f"❔ Unknown: {stations.get('unknown', 0)}",
        "",
        f"Connectors: {connector_total}",
        f"🟢 Available: {connectors.get('available', 0)}",
        f"🟠 Busy: {connectors.get('busy', 0)}",
        f"🔵 Charging: {connectors.get('charging', 0)}",
        f"🛠 Maintenance: {connectors.get('maintenance', 0)}",
    ]
    if busy_stations:
        lines.extend(["", "Currently busy stations:"])
        for station in busy_stations[:15]:
            name = station.get("name") or station.get("station_id") or "Unknown"
            available = station.get("available_connector_count", 0)
            total = station.get("connector_count", 0)
            lines.append(f"• {name} ({available}/{total} connectors available)")
    return "\n".join(lines)[:TELEGRAM_MESSAGE_LIMIT]


@dataclass
class TelegramRuntimeState:
    enabled: bool
    chat_id: int | None
    last_message_at: datetime | None = None
    last_error: str | None = None


class TelegramService:
    def __init__(
        self,
        token: str | None,
        chat_id: int | None,
        report_interval_hours: float,
        timezone_name: str,
        database: Database,
        client: TelegramClient | None = None,
    ) -> None:
        self.database = database
        self.chat_id = chat_id
        self.report_interval_seconds = report_interval_hours * 3600
        self.timezone_name = timezone_name
        self.client = client or (TelegramClient(token) if token else None)
        self.state = TelegramRuntimeState(
            enabled=bool(self.client and chat_id is not None),
            chat_id=chat_id,
        )
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()

    def start(self) -> None:
        if not self.state.enabled:
            return
        self._tasks = [
            asyncio.create_task(self._command_loop(), name="telegram-commands"),
            asyncio.create_task(self._schedule_loop(), name="telegram-scheduler"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.client:
            await self.client.close()

    async def build_report(self) -> str:
        summary = await asyncio.to_thread(self.database.summary)
        busy_stations = await asyncio.to_thread(
            self.database.list_stations,
            "busy",
            None,
            15,
            0,
        )
        return format_report(summary, busy_stations, self.timezone_name)

    async def send_report(self, prefix: str | None = None) -> dict[str, Any]:
        if not self.client or self.chat_id is None:
            raise TelegramError("Telegram service is not configured")
        report = await self.build_report()
        if prefix:
            report = f"{prefix}\n\n{report}"
        try:
            result = await self.client.send_message(self.chat_id, report)
        except Exception as exc:
            self.state.last_error = f"{type(exc).__name__}: {exc}"
            raise
        self.state.last_message_at = datetime.now(timezone.utc)
        self.state.last_error = None
        return result

    async def send_csv_export(self) -> dict[str, Any]:
        if not self.client or self.chat_id is None:
            raise TelegramError("Telegram service is not configured")
        filename, content, row_counts = await asyncio.to_thread(
            self.database.export_analytics_archive
        )
        caption = (
            "Team Energy analytics workbook + raw data\n"
            f"Stations: {row_counts.get('stations', 0)}\n"
            f"Connectors: {row_counts.get('connectors', 0)}\n"
            f"Readable status intervals: {row_counts.get('station_history', 0)}\n"
            "Revenue fields are labeled planning scenarios, not actual receipts."
        )
        try:
            result = await self.client.send_document(
                self.chat_id,
                filename,
                content,
                caption,
            )
        except Exception as exc:
            self.state.last_error = f"{type(exc).__name__}: {exc}"
            raise
        self.state.last_message_at = datetime.now(timezone.utc)
        self.state.last_error = None
        return result

    async def _handle_update(self, update: dict[str, Any]) -> None:
        if not self.client or self.chat_id is None:
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        if chat.get("id") != self.chat_id:
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.startswith("/"):
            return
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            response = (
                "Team Energy monitor commands:\n"
                "/report — current station report\n"
                "/status — collector health\n"
                "/excel — analytics workbook plus all raw CSV files\n"
                "/help — show this message"
            )
            await self.client.send_message(self.chat_id, response)
        elif command == "/report":
            await self.send_report()
        elif command == "/status":
            health = await asyncio.to_thread(self.database.health)
            last_success = health.get("last_success_at") or "not available"
            response = (
                f"Collector last success: {last_success}\n"
                f"Polls: {health.get('poll_count', 0)}\n"
                f"Stations: {health.get('station_count', 0)}\n"
                f"Connectors: {health.get('connector_count', 0)}\n"
                f"Consecutive failures: {health.get('consecutive_failures', 0)}"
            )
            await self.client.send_message(self.chat_id, response)
        elif command == "/excel":
            await self.send_csv_export()

    async def _command_loop(self) -> None:
        assert self.client is not None
        offset: int | None = None
        while not self._stop.is_set():
            try:
                updates = await self.client.get_updates(offset)
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    await self._handle_update(update)
                self.state.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("Telegram command polling failed: %s", exc)
                await asyncio.sleep(5)

    async def _schedule_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.report_interval_seconds
                )
            except TimeoutError:
                try:
                    await self.send_report()
                except Exception as exc:
                    logger.warning("Scheduled Telegram report failed: %s", exc)

    def public_status(self) -> dict[str, Any]:
        return {
            "enabled": self.state.enabled,
            "chat_configured": self.state.chat_id is not None,
            "report_interval_seconds": self.report_interval_seconds,
            "timezone": self.timezone_name,
            "last_message_at": self.state.last_message_at,
            "last_error": self.state.last_error,
        }
