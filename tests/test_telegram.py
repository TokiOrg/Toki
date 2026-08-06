from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from team_energy_service.database import Database
from team_energy_service.provider import normalize_snapshot
from team_energy_service.telegram import TelegramClient, TelegramService, format_report

from .test_database import raw_snapshot


def test_report_contains_current_counts_and_busy_station_names():
    report = format_report(
        {
            "observed_at": datetime.now(timezone.utc),
            "stations": {"available": 100, "busy": 10, "maintenance": 30},
            "connectors": {
                "available": 250,
                "busy": 10,
                "charging": 100,
                "maintenance": 50,
            },
        },
        [
            {
                "name": "Busy Station",
                "available_connector_count": 0,
                "connector_count": 2,
            }
        ],
        "Asia/Yerevan",
    )

    assert "Stations: 140" in report
    assert "Connectors: 410" in report
    assert "Busy Station" in report


def test_telegram_service_sends_report_to_configured_chat(tmp_path):
    sent_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendMessage")
        payload = __import__("json").loads(request.content)
        sent_payloads.append(payload)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 1, "chat": {"id": -100123}},
            },
        )

    database = Database(tmp_path / "telegram.duckdb")
    database.initialize()
    database.record_snapshot(
        normalize_snapshot(raw_snapshot(1), datetime.now(timezone.utc))
    )
    http_client = httpx.AsyncClient(
        base_url="https://api.telegram.org",
        transport=httpx.MockTransport(handler),
    )
    telegram_client = TelegramClient("test-token", client=http_client)
    service = TelegramService(
        token=None,
        chat_id=-100123,
        report_interval_hours=5,
        timezone_name="Asia/Yerevan",
        database=database,
        client=telegram_client,
    )

    async def run_test() -> None:
        try:
            await service.send_report(prefix="LOCAL TEST")
        finally:
            await http_client.aclose()

    asyncio.run(run_test())
    assert sent_payloads[0]["chat_id"] == -100123
    assert "LOCAL TEST" in sent_payloads[0]["text"]
    assert "Stations: 1" in sent_payloads[0]["text"]


def test_telegram_client_uploads_export_as_document():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": 2,
                    "chat": {"id": -100123},
                    "document": {"file_name": "export.zip"},
                },
            },
        )

    async def run_test() -> dict:
        http_client = httpx.AsyncClient(
            base_url="https://api.telegram.org",
            transport=httpx.MockTransport(handler),
        )
        client = TelegramClient("test-token", client=http_client)
        try:
            return await client.send_document(
                -100123,
                "export.zip",
                b"zip-content",
                "Complete export",
            )
        finally:
            await http_client.aclose()

    result = asyncio.run(run_test())
    assert result["message_id"] == 2
    assert requests[0].url.path.endswith("/sendDocument")
    assert requests[0].headers["content-type"].startswith("multipart/form-data;")
    assert b"export.zip" in requests[0].content


def test_excel_command_sends_analytics_workbook_and_raw_archive(
    tmp_path, monkeypatch
):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": 3,
                    "chat": {"id": -100123},
                    "document": {"file_name": "analytics.zip"},
                },
            },
        )

    database = Database(tmp_path / "telegram-export.duckdb")
    database.initialize()
    database.record_snapshot(
        normalize_snapshot(raw_snapshot(6), datetime.now(timezone.utc))
    )
    monkeypatch.setattr(
        "team_energy_service.database.build_analytics_workbook",
        lambda payload: b"fake-xlsx",
    )
    http_client = httpx.AsyncClient(
        base_url="https://api.telegram.org",
        transport=httpx.MockTransport(handler),
    )
    telegram_client = TelegramClient("test-token", client=http_client)
    service = TelegramService(
        token=None,
        chat_id=-100123,
        report_interval_hours=5,
        timezone_name="Asia/Yerevan",
        database=database,
        client=telegram_client,
    )

    async def run_test() -> None:
        try:
            await service.send_csv_export()
        finally:
            await http_client.aclose()

    asyncio.run(run_test())
    assert requests[0].url.path.endswith("/sendDocument")
    assert b"team_energy_analytics_and_raw_" in requests[0].content
    assert b"analytics workbook + raw data" in requests[0].content
