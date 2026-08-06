from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math

from fastapi.testclient import TestClient

from team_energy_service.api import create_app
from team_energy_service.config import Settings
from team_energy_service.provider import normalize_snapshot

from .test_database import raw_snapshot


class FakeProvider:
    async def close(self) -> None:
        return None


def test_api_returns_cached_summary_and_station_history(tmp_path):
    app = create_app(
        settings=Settings(database_path=tmp_path / "api.duckdb"),
        provider=FakeProvider(),
        start_poller=False,
    )

    with TestClient(app) as client:
        observed_at = datetime.now(timezone.utc)
        app.state.database.record_snapshot(
            normalize_snapshot(raw_snapshot(1), observed_at)
        )

        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "healthy"
        assert health.json()["connector_interval_count"] == 1

        summary = client.get("/summary")
        assert summary.status_code == 200
        assert summary.json()["connectors"] == {"available": 1}

        stations = client.get("/stations", params={"status": "available"})
        assert stations.status_code == 200
        assert stations.json()["count"] == 1
        station_id = stations.json()["stations"][0]["station_id"]

        detail = client.get(f"/stations/{station_id}")
        assert detail.status_code == 200
        assert detail.json()["connectors"][0]["status"] == "available"

        history = client.get(f"/stations/{station_id}/history")
        assert history.status_code == 200
        assert history.json()["history"][0]["status"] == "available"

        dashboard = client.get("/dashboard")
        assert dashboard.status_code == 200
        assert "Team Energy station analytics" in dashboard.text

        map_analytics = client.get("/analytics/map", params={"hours": 24})
        assert map_analytics.status_code == 200
        assert map_analytics.json()["stations"][0]["name"] == "Test Station"
        assert len(map_analytics.json()["areas"]) == 1


def test_api_returns_not_found_for_unknown_station(tmp_path):
    app = create_app(
        settings=Settings(database_path=tmp_path / "api.duckdb"),
        provider=FakeProvider(),
        start_poller=False,
    )
    with TestClient(app) as client:
        response = client.get("/stations/does-not-exist")
        assert response.status_code == 404


def test_map_analytics_clip_midpoint_history_to_selected_window(tmp_path):
    app = create_app(
        settings=Settings(database_path=tmp_path / "map.duckdb"),
        provider=FakeProvider(),
        start_poller=False,
    )
    with TestClient(app) as client:
        started = datetime.now(timezone.utc) - timedelta(minutes=1)
        for seconds, status in ((0, 1), (30, 6), (60, 6)):
            app.state.database.record_snapshot(
                normalize_snapshot(
                    raw_snapshot(status),
                    started + timedelta(seconds=seconds),
                )
            )

        response = client.get("/analytics/map", params={"hours": 1})

        assert response.status_code == 200
        data = response.json()
        station = data["stations"][0]
        assert station["current_status"] == "busy"
        assert math.isclose(station["busy_hours"], 45 / 3600, abs_tol=1e-6)
        assert math.isclose(station["busy_percent"], 0.75, abs_tol=1e-6)
        assert math.isclose(station["scenario_revenue_amd"], 75, abs_tol=1e-3)
        assert math.isclose(data["areas"][0]["busy_percent"], 0.75, abs_tol=1e-6)
        assert math.isclose(data["portfolio"]["busy_hours"], 45 / 3600, abs_tol=1e-6)
