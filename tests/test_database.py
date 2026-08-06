from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import math
import zipfile

from team_energy_service.database import Database
from team_energy_service.provider import normalize_snapshot
from team_energy_service.workbook import build_analytics_workbook


def raw_snapshot(connector_status: int) -> list[dict]:
    descriptions = {
        1: "Available",
        2: "Busy",
        4: "Maintenance",
        6: "Charging",
    }
    return [
        {
            "chargeStationId": "station-1",
            "name": "Test Station",
            "address": "Test Address",
            "latitude": 40.1,
            "longitude": 44.5,
            "isPublic": True,
            "status": "1",
            "chargePointInfos": [
                {
                    "chargePointId": "evse-1",
                    "stationName": "Test EVSE",
                    "connectors": [
                        {
                            "connectorId": "connector-1",
                            "key": "connector-key",
                            "connectorNumber": 1,
                            "connectorType": "CCS2",
                            "connectorTypeGroup": "DC",
                            "power": 120,
                            "price": 100,
                            "status": connector_status,
                            "statusDescription": descriptions[connector_status],
                            "stateOfBattery": 0,
                            "isPrepairing": False,
                        }
                    ],
                }
            ],
        }
    ]


def test_unchanged_polls_do_not_create_duplicate_intervals(tmp_path):
    database = Database(tmp_path / "history.duckdb")
    database.initialize()
    started = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)

    first = database.record_snapshot(normalize_snapshot(raw_snapshot(1), started))
    second = database.record_snapshot(
        normalize_snapshot(raw_snapshot(1), started + timedelta(seconds=30))
    )

    assert first["connector_changes"] == 1
    assert first["station_changes"] == 1
    assert second["connector_changes"] == 0
    assert second["station_changes"] == 0
    health = database.health()
    assert health["connector_interval_count"] == 1
    assert health["station_interval_count"] == 1
    assert health["poll_count"] == 2
    history = database.connector_history("connector-1", 24 * 366)
    assert history[0]["last_confirmed_at"] == started + timedelta(seconds=30)


def test_status_change_closes_old_intervals_with_observation_bounds(tmp_path):
    database = Database(tmp_path / "history.duckdb")
    database.initialize()
    first_at = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)
    confirmed_at = first_at + timedelta(seconds=30)
    changed_at = first_at + timedelta(seconds=60)

    database.record_snapshot(normalize_snapshot(raw_snapshot(1), first_at))
    database.record_snapshot(normalize_snapshot(raw_snapshot(1), confirmed_at))
    result = database.record_snapshot(normalize_snapshot(raw_snapshot(6), changed_at))

    assert result["connector_changes"] == 1
    assert result["station_changes"] == 1
    connector_history = database.connector_history("connector-1", 24 * 366)
    assert [row["status"] for row in connector_history] == ["charging", "available"]
    assert connector_history[1]["last_confirmed_at"] == confirmed_at
    assert connector_history[1]["ended_at"] == changed_at
    station_history = database.station_history("station-1", 24 * 366)
    assert [row["status"] for row in station_history] == ["busy", "available"]


def test_complete_database_export_contains_excel_compatible_csvs(tmp_path):
    database = Database(tmp_path / "history.duckdb")
    database.initialize()
    database.record_snapshot(
        normalize_snapshot(
            raw_snapshot(1),
            datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc),
        )
    )

    filename, content, counts = database.export_csv_archive()

    assert filename.startswith("team_energy_all_data_")
    assert filename.endswith(".zip")
    assert counts["stations"] == 1
    assert counts["connectors"] == 1
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert set(archive.namelist()) == {
            "stations.csv",
            "connectors.csv",
            "station_status_intervals.csv",
            "connector_status_intervals.csv",
            "collector_state.csv",
            "collector_gaps.csv",
            "README.txt",
        }
        assert archive.read("stations.csv").startswith(b"\xef\xbb\xbf")
        assert b"Test Station" in archive.read("stations.csv")


def test_analytics_use_midpoint_intervals_and_charging_only_for_revenue(tmp_path):
    database = Database(tmp_path / "analytics.duckdb")
    database.initialize()
    started = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)
    observations = [
        (0, 1),
        (30, 1),
        (60, 6),
        (90, 6),
        (120, 1),
    ]
    for seconds, status in observations:
        database.record_snapshot(
            normalize_snapshot(
                raw_snapshot(status),
                started + timedelta(seconds=seconds),
            )
        )

    payload = database.analytics_payload()
    analytics = payload["station_analytics"][0]

    assert analytics["station_name"] == "Test Station"
    assert math.isclose(analytics["busy_hours"], 60 / 3600, abs_tol=1e-6)
    assert math.isclose(
        analytics["charging_connector_hours"], 60 / 3600, abs_tol=1e-6
    )
    assert math.isclose(
        analytics["rated_energy_ceiling_kwh"], 2.0, abs_tol=1e-5
    )
    assert math.isclose(
        analytics["rated_power_revenue_ceiling_amd"], 200.0, abs_tol=1e-4
    )
    assert math.isclose(analytics["scenario_revenue_amd"], 100.0, abs_tol=1e-4)
    assert [row["status"] for row in payload["station_history"]] == [
        "available",
        "busy",
        "available",
    ]
    assert payload["station_history"][1]["transition_uncertainty_minutes"] == 0.5
    assert all(
        row["metadata_basis"] == "observed_at_interval_start"
        for row in payload["connector_history"]
    )


def test_analytics_archive_keeps_workbook_readable_csvs_and_raw_data(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "analytics-export.duckdb")
    database.initialize()
    database.record_snapshot(
        normalize_snapshot(
            raw_snapshot(1),
            datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc),
        )
    )
    monkeypatch.setattr(
        "team_energy_service.database.build_analytics_workbook",
        lambda payload: b"fake-xlsx",
    )

    filename, content, counts = database.export_analytics_archive()

    assert filename.startswith("team_energy_analytics_and_raw_")
    assert counts["station_analytics"] == 1
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = set(archive.namelist())
        assert any(name.endswith(".xlsx") for name in names)
        assert "station_analytics.csv" in names
        assert "readable_station_history.csv" in names
        assert "readable_connector_history.csv" in names
        assert "stations.csv" in names
        assert "connector_status_intervals.csv" in names
        assert b"Test Station" in archive.read("station_analytics.csv")
        assert b"scenario_revenue_amd" in archive.read("station_analytics.csv")
        assert "collector_gaps.csv" in names


def test_portable_workbook_keeps_sheets_formulas_chart_and_unicode(tmp_path):
    database = Database(tmp_path / "workbook.duckdb")
    database.initialize()
    snapshot = raw_snapshot(6)
    snapshot[0]["name"] = "Թեստային կայան"
    snapshot[0]["address"] = "Երևան"
    database.record_snapshot(
        normalize_snapshot(
            snapshot,
            datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc),
        )
    )

    workbook = build_analytics_workbook(database.analytics_payload())

    assert workbook.startswith(b"PK")
    with zipfile.ZipFile(io.BytesIO(workbook)) as archive:
        workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
        expected_sheets = [
            "Dashboard",
            "Station Analytics",
            "Readable History",
            "Connector History",
            "Methodology",
            "Raw Stations",
            "Raw Connectors",
            "Raw Station History",
            "Raw Connector History",
            "Collector State",
            "Collector Gaps",
        ]
        positions = [workbook_xml.index(f'name="{name}"') for name in expected_sheets]
        assert positions == sorted(positions)
        assert "xl/charts/chart1.xml" in archive.namelist()
        assert "Թեստային կայան" in archive.read("xl/sharedStrings.xml").decode("utf-8")
        worksheet_xml = "\n".join(
            archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        )
        assert "'Methodology'!$B$4" in worksheet_xml
        assert "SUM(I2:L2)" in worksheet_xml
        assert "IFERROR(M2/H2,0)" in worksheet_xml


def test_long_collection_gap_is_recorded_as_unknown_and_excluded_from_utilization(
    tmp_path,
):
    database = Database(tmp_path / "gaps.duckdb", gap_threshold_seconds=120)
    database.initialize()
    started = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)

    results = []
    for seconds, status in ((0, 1), (30, 1), (330, 6), (360, 6)):
        results.append(database.record_snapshot(
            normalize_snapshot(
                raw_snapshot(status),
                started + timedelta(seconds=seconds),
            )
        ))

    assert [result["gaps_created"] for result in results] == [0, 0, 1, 0]
    gaps = database.list_collector_gaps(24 * 366)
    assert len(gaps) == 1
    assert gaps[0]["reason"] == "collector_restart_or_suspension"
    assert math.isclose(gaps[0]["duration_seconds"], 300, abs_tol=0.01)
    assert gaps[0]["station_intervals_marked"] == 1
    assert gaps[0]["connector_intervals_marked"] == 1

    station_history = database.station_history("station-1", 24 * 366)
    assert [row["status"] for row in station_history] == [
        "busy",
        "unknown",
        "available",
    ]
    connector_history = database.connector_history("connector-1", 24 * 366)
    assert [row["status"] for row in connector_history] == [
        "charging",
        "unknown",
        "available",
    ]

    analytics = database.analytics_payload()["station_analytics"][0]
    assert math.isclose(analytics["unknown_hours"], 300 / 3600, abs_tol=1e-6)
    assert math.isclose(analytics["known_station_hours"], 60 / 3600, abs_tol=1e-6)
    assert math.isclose(analytics["coverage_percent"], 1 / 6, abs_tol=1e-5)
    assert math.isclose(analytics["busy_percent"], 0.5, abs_tol=1e-5)

    dashboard = database.dashboard_analytics(hours=None)
    station = dashboard["stations"][0]
    area = dashboard["areas"][0]
    portfolio = dashboard["portfolio"]
    assert math.isclose(station["known_hours"], 60 / 3600, abs_tol=1e-6)
    assert math.isclose(station["unknown_hours"], 300 / 3600, abs_tol=1e-6)
    assert math.isclose(station["coverage_percent"], 1 / 6, abs_tol=1e-5)
    assert math.isclose(station["busy_percent"], 0.5, abs_tol=1e-5)
    for row in (area, portfolio):
        assert math.isclose(
            row["known_station_hours"], 60 / 3600, abs_tol=1e-6
        )
        assert math.isclose(
            row["unknown_station_hours"], 300 / 3600, abs_tol=1e-6
        )
        assert math.isclose(row["coverage_percent"], 1 / 6, abs_tol=1e-5)
        assert math.isclose(row["busy_percent"], 0.5, abs_tol=1e-5)

    health = database.health()
    assert health["collector_gap_count"] == 1
    assert health["latest_collector_gap"]["gap_id"] == gaps[0]["gap_id"]
