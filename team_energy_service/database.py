from __future__ import annotations

import csv
import hashlib
import io
import math
import threading
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import duckdb

from .provider import Snapshot
from .workbook import build_analytics_workbook


EXPORT_TABLES = {
    "stations.csv": ("stations", "station_id"),
    "connectors.csv": ("connectors", "connector_id"),
    "station_status_intervals.csv": (
        "station_status_intervals",
        "station_id, started_at",
    ),
    "connector_status_intervals.csv": (
        "connector_status_intervals",
        "connector_id, started_at",
    ),
    "collector_state.csv": ("collector_state", "singleton_id"),
    "collector_gaps.csv": ("collector_gaps", "started_at"),
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    station_id VARCHAR PRIMARY KEY,
    name VARCHAR,
    address VARCHAR,
    phone VARCHAR,
    latitude DOUBLE,
    longitude DOUBLE,
    is_public BOOLEAN,
    source_status_code VARCHAR,
    current_status VARCHAR NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS connectors (
    connector_id VARCHAR PRIMARY KEY,
    station_id VARCHAR NOT NULL,
    evse_id VARCHAR,
    connector_key VARCHAR,
    evse_name VARCHAR,
    connector_number INTEGER,
    connector_type VARCHAR,
    connector_type_group VARCHAR,
    power_kw DOUBLE,
    price DOUBLE,
    status_code VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    status_description VARCHAR,
    state_of_battery DOUBLE,
    is_preparing BOOLEAN,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS connector_status_intervals (
    connector_id VARCHAR NOT NULL,
    station_id VARCHAR NOT NULL,
    status_code VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    status_description VARCHAR,
    power_kw DOUBLE,
    price DOUBLE,
    metadata_basis VARCHAR,
    battery_at_start DOUBLE,
    battery_at_end DOUBLE,
    started_at TIMESTAMPTZ NOT NULL,
    last_confirmed_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    PRIMARY KEY (connector_id, started_at)
);

CREATE TABLE IF NOT EXISTS station_status_intervals (
    station_id VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    last_confirmed_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    PRIMARY KEY (station_id, started_at)
);

CREATE TABLE IF NOT EXISTS collector_state (
    singleton_id INTEGER PRIMARY KEY,
    last_attempt_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_error VARCHAR,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    poll_count BIGINT NOT NULL DEFAULT 0,
    last_response_hash VARCHAR
);

CREATE TABLE IF NOT EXISTS collector_gaps (
    gap_id VARCHAR PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ NOT NULL,
    duration_seconds DOUBLE NOT NULL,
    reason VARCHAR NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL,
    station_intervals_marked BIGINT NOT NULL,
    connector_intervals_marked BIGINT NOT NULL
);

INSERT INTO collector_state (singleton_id)
VALUES (1)
ON CONFLICT DO NOTHING;
"""


def _rows_as_dicts(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    parameters: Iterable[Any] | None = None,
) -> list[dict[str, Any]]:
    cursor = connection.execute(sql, parameters or [])
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


class Database:
    """Serialized access to one file-backed DuckDB database."""

    def __init__(self, path: Path, gap_threshold_seconds: float = 120.0) -> None:
        self.path = path
        self.gap_threshold_seconds = gap_threshold_seconds
        self._lock = threading.RLock()

    def _connect(self) -> duckdb.DuckDBPyConnection:
        connection = duckdb.connect(str(self.path))
        connection.execute("SET TimeZone = 'UTC'")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.execute(SCHEMA)
            connection.execute(
                "ALTER TABLE connector_status_intervals "
                "ADD COLUMN IF NOT EXISTS power_kw DOUBLE"
            )
            connection.execute(
                "ALTER TABLE connector_status_intervals "
                "ADD COLUMN IF NOT EXISTS price DOUBLE"
            )
            connection.execute(
                "ALTER TABLE connector_status_intervals "
                "ADD COLUMN IF NOT EXISTS metadata_basis VARCHAR"
            )
            connection.execute(
                "ALTER TABLE connector_status_intervals "
                "ADD COLUMN IF NOT EXISTS battery_at_start DOUBLE"
            )
            connection.execute(
                "ALTER TABLE connector_status_intervals "
                "ADD COLUMN IF NOT EXISTS battery_at_end DOUBLE"
            )
            connection.execute(
                """
                UPDATE connector_status_intervals AS history
                SET power_kw = coalesce(history.power_kw, connectors.power_kw),
                    price = coalesce(history.price, connectors.price),
                    metadata_basis = coalesce(
                        history.metadata_basis,
                        'backfilled_from_current_connector'
                    )
                FROM connectors
                WHERE history.connector_id = connectors.connector_id
                  AND (
                      history.power_kw IS NULL
                      OR history.price IS NULL
                      OR history.metadata_basis IS NULL
                  )
                """
            )

    def record_attempt(self, attempted_at: datetime) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE collector_state
                SET last_attempt_at = ?
                WHERE singleton_id = 1
                """,
                [attempted_at],
            )

    def record_failure(self, attempted_at: datetime, error: str) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                UPDATE collector_state
                SET last_attempt_at = ?,
                    last_error = ?,
                    consecutive_failures = consecutive_failures + 1
                WHERE singleton_id = 1
                RETURNING consecutive_failures
                """,
                [attempted_at, error[:1000]],
            ).fetchone()
            return int(row[0]) if row else 1

    def record_snapshot(self, snapshot: Snapshot) -> dict[str, int]:
        observed_at = snapshot.observed_at
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                state = connection.execute(
                    """
                    SELECT last_success_at, consecutive_failures
                    FROM collector_state
                    WHERE singleton_id = 1
                    """
                ).fetchone()
                previous_success = state[0] if state and state[0] else observed_at
                failures_before_recovery = int(state[1] or 0) if state else 0
                gap_result = self._record_gap_if_needed(
                    connection,
                    previous_success,
                    observed_at,
                    failures_before_recovery,
                )
                connector_changes = self._record_connectors(
                    connection, snapshot, previous_success
                )
                station_changes = self._record_stations(
                    connection, snapshot, previous_success
                )
                connection.execute(
                    """
                    UPDATE collector_state
                    SET last_attempt_at = ?,
                        last_success_at = ?,
                        last_error = NULL,
                        consecutive_failures = 0,
                        poll_count = poll_count + 1,
                        last_response_hash = ?
                    WHERE singleton_id = 1
                    """,
                    [observed_at, observed_at, snapshot.raw_response_hash],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

        return {
            "stations": len(snapshot.stations),
            "connectors": len(snapshot.connectors),
            "station_changes": station_changes,
            "connector_changes": connector_changes,
            "gaps_created": int(gap_result is not None),
        }

    def _record_gap_if_needed(
        self,
        connection: duckdb.DuckDBPyConnection,
        previous_success: datetime,
        observed_at: datetime,
        failures_before_recovery: int,
    ) -> dict[str, int] | None:
        elapsed_seconds = (observed_at - previous_success).total_seconds()
        if elapsed_seconds <= self.gap_threshold_seconds:
            return None

        gap_start = previous_success + timedelta(microseconds=1)
        if gap_start >= observed_at:
            return None
        station_rows = _rows_as_dicts(
            connection,
            """
            SELECT station_id
            FROM station_status_intervals
            WHERE ended_at IS NULL
            ORDER BY station_id
            """,
        )
        connector_rows = _rows_as_dicts(
            connection,
            """
            SELECT connector_id, station_id
            FROM connector_status_intervals
            WHERE ended_at IS NULL
            ORDER BY connector_id
            """,
        )

        connection.execute(
            """
            UPDATE station_status_intervals
            SET last_confirmed_at = ?, ended_at = ?
            WHERE ended_at IS NULL
            """,
            [previous_success, gap_start],
        )
        connection.execute(
            """
            UPDATE connector_status_intervals
            SET last_confirmed_at = ?, ended_at = ?
            WHERE ended_at IS NULL
            """,
            [previous_success, gap_start],
        )
        for row in station_rows:
            connection.execute(
                """
                INSERT INTO station_status_intervals (
                    station_id, status, started_at, last_confirmed_at, ended_at
                ) VALUES (?, 'unknown', ?, ?, ?)
                """,
                [row["station_id"], gap_start, observed_at, observed_at],
            )
        for row in connector_rows:
            connection.execute(
                """
                INSERT INTO connector_status_intervals (
                    connector_id, station_id, status_code, status,
                    status_description, power_kw, price, metadata_basis,
                    started_at, last_confirmed_at, ended_at
                ) VALUES (?, ?, 'collector_offline', 'unknown', ?, NULL, NULL,
                          'automatic_collector_gap', ?, ?, ?)
                """,
                [
                    row["connector_id"],
                    row["station_id"],
                    "Collector unavailable between successful polls",
                    gap_start,
                    observed_at,
                    observed_at,
                ],
            )

        reason = (
            "poll_failures"
            if failures_before_recovery
            else "collector_restart_or_suspension"
        )
        gap_id = hashlib.sha256(
            f"{gap_start.isoformat()}|{observed_at.isoformat()}".encode("utf-8")
        ).hexdigest()
        connection.execute(
            """
            INSERT INTO collector_gaps (
                gap_id, started_at, ended_at, duration_seconds, reason,
                detected_at, station_intervals_marked,
                connector_intervals_marked
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                gap_id,
                gap_start,
                observed_at,
                (observed_at - gap_start).total_seconds(),
                reason,
                datetime.now(timezone.utc),
                len(station_rows),
                len(connector_rows),
            ],
        )
        return {
            "station_intervals_marked": len(station_rows),
            "connector_intervals_marked": len(connector_rows),
        }

    def _record_connectors(
        self,
        connection: duckdb.DuckDBPyConnection,
        snapshot: Snapshot,
        previous_success: datetime,
    ) -> int:
        current_rows = _rows_as_dicts(connection, "SELECT * FROM connectors")
        current = {row["connector_id"]: row for row in current_rows}
        open_rows = _rows_as_dicts(
            connection,
            """
            SELECT connector_id, status_code, status
            FROM connector_status_intervals
            WHERE ended_at IS NULL
            """,
        )
        open_intervals = {row["connector_id"]: row for row in open_rows}
        changes = 0

        fields = [
            "station_id",
            "evse_id",
            "connector_key",
            "evse_name",
            "connector_number",
            "connector_type",
            "connector_type_group",
            "power_kw",
            "price",
            "status_code",
            "status",
            "status_description",
            "state_of_battery",
            "is_preparing",
        ]
        for connector in snapshot.connectors:
            connector_id = connector["connector_id"]
            old = current.get(connector_id)
            values = [connector.get(field) for field in fields]
            if old is None:
                placeholders = ", ".join("?" for _ in range(len(fields) + 2))
                connection.execute(
                    f"""
                    INSERT INTO connectors (
                        connector_id, {", ".join(fields)}, updated_at
                    ) VALUES ({placeholders})
                    """,
                    [connector_id, *values, snapshot.observed_at],
                )
            elif any(old.get(field) != connector.get(field) for field in fields):
                assignments = ", ".join(f"{field} = ?" for field in fields)
                connection.execute(
                    f"""
                    UPDATE connectors
                    SET {assignments}, updated_at = ?
                    WHERE connector_id = ?
                    """,
                    [*values, snapshot.observed_at, connector_id],
                )

            battery = connector.get("state_of_battery")
            open_interval = open_intervals.get(connector_id)
            if open_interval is None:
                connection.execute(
                    """
                    INSERT INTO connector_status_intervals (
                        connector_id, station_id, status_code, status,
                        status_description, power_kw, price, metadata_basis,
                        battery_at_start, battery_at_end,
                        started_at, last_confirmed_at, ended_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, 'observed_at_interval_start',
                        ?, ?, ?, ?, NULL
                    )
                    """,
                    [
                        connector_id,
                        connector["station_id"],
                        connector["status_code"],
                        connector["status"],
                        connector.get("status_description"),
                        connector.get("power_kw"),
                        connector.get("price"),
                        battery,
                        battery,
                        snapshot.observed_at,
                        snapshot.observed_at,
                    ],
                )
                changes += 1
            elif open_interval["status_code"] != connector["status_code"]:
                connection.execute(
                    """
                    UPDATE connector_status_intervals
                    SET last_confirmed_at = ?, ended_at = ?
                    WHERE connector_id = ? AND ended_at IS NULL
                    """,
                    [previous_success, snapshot.observed_at, connector_id],
                )
                connection.execute(
                    """
                    INSERT INTO connector_status_intervals (
                        connector_id, station_id, status_code, status,
                        status_description, power_kw, price, metadata_basis,
                        battery_at_start, battery_at_end,
                        started_at, last_confirmed_at, ended_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, 'observed_at_interval_start',
                        ?, ?, ?, ?, NULL
                    )
                    """,
                    [
                        connector_id,
                        connector["station_id"],
                        connector["status_code"],
                        connector["status"],
                        connector.get("status_description"),
                        connector.get("power_kw"),
                        connector.get("price"),
                        battery,
                        battery,
                        snapshot.observed_at,
                        snapshot.observed_at,
                    ],
                )
                changes += 1
            elif battery is not None:
                connection.execute(
                    """
                    UPDATE connector_status_intervals
                    SET battery_at_end = ?,
                        battery_at_start = coalesce(battery_at_start, ?)
                    WHERE connector_id = ? AND ended_at IS NULL
                    """,
                    [battery, battery, connector_id],
                )
        return changes

    def _record_stations(
        self,
        connection: duckdb.DuckDBPyConnection,
        snapshot: Snapshot,
        previous_success: datetime,
    ) -> int:
        current_rows = _rows_as_dicts(connection, "SELECT * FROM stations")
        current = {row["station_id"]: row for row in current_rows}
        open_rows = _rows_as_dicts(
            connection,
            """
            SELECT station_id, status
            FROM station_status_intervals
            WHERE ended_at IS NULL
            """,
        )
        open_intervals = {row["station_id"]: row for row in open_rows}
        changes = 0
        fields = [
            "name",
            "address",
            "phone",
            "latitude",
            "longitude",
            "is_public",
            "source_status_code",
            "current_status",
        ]

        for station in snapshot.stations:
            station_id = station["station_id"]
            old = current.get(station_id)
            values = [station.get(field) for field in fields]
            if old is None:
                placeholders = ", ".join("?" for _ in range(len(fields) + 2))
                connection.execute(
                    f"""
                    INSERT INTO stations (
                        station_id, {", ".join(fields)}, updated_at
                    ) VALUES ({placeholders})
                    """,
                    [station_id, *values, snapshot.observed_at],
                )
            elif any(old.get(field) != station.get(field) for field in fields):
                assignments = ", ".join(f"{field} = ?" for field in fields)
                connection.execute(
                    f"""
                    UPDATE stations
                    SET {assignments}, updated_at = ?
                    WHERE station_id = ?
                    """,
                    [*values, snapshot.observed_at, station_id],
                )

            open_interval = open_intervals.get(station_id)
            if open_interval is None:
                connection.execute(
                    """
                    INSERT INTO station_status_intervals (
                        station_id, status, started_at, last_confirmed_at, ended_at
                    ) VALUES (?, ?, ?, ?, NULL)
                    """,
                    [
                        station_id,
                        station["current_status"],
                        snapshot.observed_at,
                        snapshot.observed_at,
                    ],
                )
                changes += 1
            elif open_interval["status"] != station["current_status"]:
                connection.execute(
                    """
                    UPDATE station_status_intervals
                    SET last_confirmed_at = ?, ended_at = ?
                    WHERE station_id = ? AND ended_at IS NULL
                    """,
                    [previous_success, snapshot.observed_at, station_id],
                )
                connection.execute(
                    """
                    INSERT INTO station_status_intervals (
                        station_id, status, started_at, last_confirmed_at, ended_at
                    ) VALUES (?, ?, ?, ?, NULL)
                    """,
                    [
                        station_id,
                        station["current_status"],
                        snapshot.observed_at,
                        snapshot.observed_at,
                    ],
                )
                changes += 1
        return changes

    def health(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            state = _rows_as_dicts(
                connection,
                "SELECT * FROM collector_state WHERE singleton_id = 1",
            )[0]
            counts = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM stations) AS station_count,
                    (SELECT count(*) FROM connectors) AS connector_count,
                    (SELECT count(*) FROM station_status_intervals) AS station_intervals,
                    (SELECT count(*) FROM connector_status_intervals) AS connector_intervals,
                    (SELECT count(*) FROM collector_gaps) AS collector_gaps
                """
            ).fetchone()
            latest_gap_rows = _rows_as_dicts(
                connection,
                """
                SELECT *
                FROM collector_gaps
                ORDER BY ended_at DESC
                LIMIT 1
                """,
            )
        state.update(
            {
                "station_count": counts[0],
                "connector_count": counts[1],
                "station_interval_count": counts[2],
                "connector_interval_count": counts[3],
                "collector_gap_count": counts[4],
                "latest_collector_gap": latest_gap_rows[0]
                if latest_gap_rows
                else None,
            }
        )
        return state

    def list_collector_gaps(self, hours: int = 24 * 30) -> list[dict[str, Any]]:
        cutoff_at = datetime.now(timezone.utc) - timedelta(hours=hours)
        with self._lock, self._connect() as connection:
            return _rows_as_dicts(
                connection,
                """
                SELECT *
                FROM collector_gaps
                WHERE ended_at >= ?
                ORDER BY ended_at DESC
                """,
                [cutoff_at],
            )

    def summary(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            station_rows = connection.execute(
                """
                SELECT current_status, count(*)
                FROM stations
                GROUP BY current_status
                ORDER BY current_status
                """
            ).fetchall()
            connector_rows = connection.execute(
                """
                SELECT status, count(*)
                FROM connectors
                GROUP BY status
                ORDER BY status
                """
            ).fetchall()
            state = connection.execute(
                """
                SELECT last_success_at
                FROM collector_state
                WHERE singleton_id = 1
                """
            ).fetchone()
        return {
            "observed_at": state[0] if state else None,
            "stations": {row[0]: row[1] for row in station_rows},
            "connectors": {row[0]: row[1] for row in connector_rows},
        }

    def list_stations(
        self,
        status: str | None = None,
        query: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if status:
            clauses.append("s.current_status = ?")
            parameters.append(status)
        if query:
            clauses.append(
                "(lower(s.name) LIKE lower(?) OR lower(s.address) LIKE lower(?))"
            )
            pattern = f"%{query}%"
            parameters.extend([pattern, pattern])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend([limit, offset])

        with self._lock, self._connect() as connection:
            return _rows_as_dicts(
                connection,
                f"""
                SELECT
                    s.*,
                    count(c.connector_id) AS connector_count,
                    count(c.connector_id) FILTER (WHERE c.status = 'available')
                        AS available_connector_count
                FROM stations s
                LEFT JOIN connectors c ON c.station_id = s.station_id
                {where}
                GROUP BY ALL
                ORDER BY s.name, s.station_id
                LIMIT ? OFFSET ?
                """,
                parameters,
            )

    def get_station(self, station_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            stations = _rows_as_dicts(
                connection,
                "SELECT * FROM stations WHERE station_id = ?",
                [station_id],
            )
            if not stations:
                return None
            connectors = _rows_as_dicts(
                connection,
                """
                SELECT *
                FROM connectors
                WHERE station_id = ?
                ORDER BY evse_id, connector_number, connector_id
                """,
                [station_id],
            )
        station = stations[0]
        station["connectors"] = connectors
        return station

    def station_history(self, station_id: str, hours: int) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc).timestamp() - (hours * 3600)
        cutoff_at = datetime.fromtimestamp(cutoff, timezone.utc)
        with self._lock, self._connect() as connection:
            return _rows_as_dicts(
                connection,
                """
                SELECT
                    station_id,
                    status,
                    started_at,
                    CASE
                        WHEN ended_at IS NULL THEN
                            (SELECT last_success_at FROM collector_state WHERE singleton_id = 1)
                        ELSE last_confirmed_at
                    END AS last_confirmed_at,
                    ended_at,
                    date_diff(
                        'second',
                        started_at,
                        coalesce(
                            ended_at,
                            (SELECT last_success_at FROM collector_state WHERE singleton_id = 1)
                        )
                    ) AS confirmed_duration_seconds
                FROM station_status_intervals
                WHERE station_id = ?
                  AND coalesce(ended_at, current_timestamp) >= ?
                ORDER BY started_at DESC
                """,
                [station_id, cutoff_at],
            )

    def connector_history(
        self, connector_id: str, hours: int
    ) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc).timestamp() - (hours * 3600)
        cutoff_at = datetime.fromtimestamp(cutoff, timezone.utc)
        with self._lock, self._connect() as connection:
            return _rows_as_dicts(
                connection,
                """
                SELECT
                    connector_id,
                    station_id,
                    status_code,
                    status,
                    status_description,
                    started_at,
                    CASE
                        WHEN ended_at IS NULL THEN
                            (SELECT last_success_at FROM collector_state WHERE singleton_id = 1)
                        ELSE last_confirmed_at
                    END AS last_confirmed_at,
                    ended_at,
                    date_diff(
                        'second',
                        started_at,
                        coalesce(
                            ended_at,
                            (SELECT last_success_at FROM collector_state WHERE singleton_id = 1)
                        )
                    ) AS confirmed_duration_seconds
                FROM connector_status_intervals
                WHERE connector_id = ?
                  AND coalesce(ended_at, current_timestamp) >= ?
                ORDER BY started_at DESC
                """,
                [connector_id, cutoff_at],
            )

    @staticmethod
    def _build_connector_analytics(
        connector_rows: list[dict[str, Any]],
        station_metrics: dict[str, dict[str, Any]],
        connector_history: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Per-connector decision metrics, mirroring the station analytics.

        Sessions served is the count of charging intervals for the connector.
        Battery-in/out is averaged across charging sessions that recorded a
        reading; it is only populated for sessions collected after the battery
        columns were deployed, so older sessions leave it blank.
        """
        scenario_load_factor = 0.5
        metrics: dict[str, dict[str, Any]] = {}
        for connector in connector_rows:
            connector_id = connector["connector_id"]
            station_id = connector.get("station_id")
            station = station_metrics.get(station_id, {})
            metrics[connector_id] = {
                "station_name": station.get("station_name") or station_id,
                "address": station.get("address"),
                "evse_name": connector.get("evse_name"),
                "connector_number": connector.get("connector_number"),
                "connector_type": connector.get("connector_type"),
                "connector_type_group": connector.get("connector_type_group"),
                "current_status": connector.get("status"),
                "power_kw": connector.get("power_kw"),
                "price": connector.get("price"),
                "observation_start": None,
                "observation_end": None,
                "available_hours": 0.0,
                "busy_hours": 0.0,
                "charging_hours": 0.0,
                "maintenance_hours": 0.0,
                "unknown_hours": 0.0,
                "observed_connector_hours": 0.0,
                "known_connector_hours": 0.0,
                "coverage_percent": 0.0,
                "busy_percent": 0.0,
                "availability_percent": 0.0,
                "charging_percent": 0.0,
                "cars_served": 0,
                "charging_connector_hours": 0.0,
                "rated_energy_ceiling_kwh": 0.0,
                "energy_weighted_price": None,
                "scenario_energy_kwh": 0.0,
                "scenario_revenue_amd": 0.0,
                "rated_power_revenue_ceiling_amd": 0.0,
                "avg_battery_in_percent": None,
                "avg_battery_out_percent": None,
                "avg_battery_delta_percent": None,
                "transition_uncertainty_minutes": 0.0,
                "connector_id": connector_id,
                "station_id": station_id,
                "_price_numerator": 0.0,
                "_battery_in_sum": 0.0,
                "_battery_in_count": 0,
                "_battery_out_sum": 0.0,
                "_battery_out_count": 0,
            }

        for interval in connector_history:
            metric = metrics.get(interval["connector_id"])
            if metric is None:
                continue
            status = interval.get("status")
            duration = float(interval.get("midpoint_estimated_hours") or 0)
            hours_key = f"{status}_hours"
            if hours_key not in metric:
                hours_key = "unknown_hours"
            metric[hours_key] += duration
            metric["transition_uncertainty_minutes"] += float(
                interval.get("transition_uncertainty_minutes") or 0
            )
            estimated_start = interval.get("estimated_started_at")
            estimated_end = interval.get("estimated_ended_at")
            if estimated_start is not None and (
                metric["observation_start"] is None
                or estimated_start < metric["observation_start"]
            ):
                metric["observation_start"] = estimated_start
            if estimated_end is not None and (
                metric["observation_end"] is None
                or estimated_end > metric["observation_end"]
            ):
                metric["observation_end"] = estimated_end

            if status == "charging":
                power_kw = float(interval.get("power_kw") or 0)
                price = float(interval.get("price") or 0)
                rated_energy = duration * power_kw
                metric["cars_served"] += 1
                metric["charging_connector_hours"] += duration
                metric["rated_energy_ceiling_kwh"] += rated_energy
                metric["rated_power_revenue_ceiling_amd"] += rated_energy * price
                metric["_price_numerator"] += rated_energy * price
                battery_in = interval.get("battery_at_start")
                battery_out = interval.get("battery_at_end")
                if battery_in is not None:
                    metric["_battery_in_sum"] += float(battery_in)
                    metric["_battery_in_count"] += 1
                if battery_out is not None:
                    metric["_battery_out_sum"] += float(battery_out)
                    metric["_battery_out_count"] += 1

        connector_analytics = list(metrics.values())
        for metric in connector_analytics:
            observed = sum(
                float(metric[key])
                for key in (
                    "available_hours",
                    "busy_hours",
                    "charging_hours",
                    "maintenance_hours",
                    "unknown_hours",
                )
            )
            known = observed - float(metric["unknown_hours"])
            metric["observed_connector_hours"] = observed
            metric["known_connector_hours"] = known
            metric["coverage_percent"] = known / observed if observed else 0.0
            metric["busy_percent"] = (
                (float(metric["busy_hours"]) + float(metric["charging_hours"]))
                / known
                if known
                else 0.0
            )
            metric["availability_percent"] = (
                float(metric["available_hours"]) / known if known else 0.0
            )
            metric["charging_percent"] = (
                float(metric["charging_hours"]) / known if known else 0.0
            )
            energy = metric["rated_energy_ceiling_kwh"]
            if energy > 0:
                metric["energy_weighted_price"] = metric["_price_numerator"] / energy
            metric["scenario_energy_kwh"] = (
                metric["rated_energy_ceiling_kwh"] * scenario_load_factor
            )
            metric["scenario_revenue_amd"] = (
                metric["rated_power_revenue_ceiling_amd"] * scenario_load_factor
            )
            if metric["_battery_in_count"]:
                metric["avg_battery_in_percent"] = (
                    metric["_battery_in_sum"] / metric["_battery_in_count"]
                )
            if metric["_battery_out_count"]:
                metric["avg_battery_out_percent"] = (
                    metric["_battery_out_sum"] / metric["_battery_out_count"]
                )
            if (
                metric["avg_battery_in_percent"] is not None
                and metric["avg_battery_out_percent"] is not None
            ):
                metric["avg_battery_delta_percent"] = (
                    metric["avg_battery_out_percent"]
                    - metric["avg_battery_in_percent"]
                )
            for key in (
                "available_hours",
                "busy_hours",
                "charging_hours",
                "maintenance_hours",
                "unknown_hours",
                "observed_connector_hours",
                "known_connector_hours",
                "coverage_percent",
                "busy_percent",
                "availability_percent",
                "charging_percent",
                "charging_connector_hours",
                "rated_energy_ceiling_kwh",
                "rated_power_revenue_ceiling_amd",
                "scenario_energy_kwh",
                "scenario_revenue_amd",
                "transition_uncertainty_minutes",
            ):
                metric[key] = round(float(metric[key]), 6)
            for key in (
                "energy_weighted_price",
                "avg_battery_in_percent",
                "avg_battery_out_percent",
                "avg_battery_delta_percent",
            ):
                if metric[key] is not None:
                    metric[key] = round(float(metric[key]), 6)
            for helper_key in (
                "_price_numerator",
                "_battery_in_sum",
                "_battery_in_count",
                "_battery_out_sum",
                "_battery_out_count",
            ):
                metric.pop(helper_key, None)

        connector_analytics.sort(
            key=lambda metric: (
                -metric["charging_connector_hours"],
                -metric["busy_hours"],
                metric["station_name"] or "",
                metric["connector_id"],
            )
        )
        return connector_analytics

    def analytics_payload(self) -> dict[str, Any]:
        """Build readable status history and station-level decision metrics."""
        generated_at = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            raw_tables = {
                table: _rows_as_dicts(
                    connection,
                    f"SELECT * FROM {table} ORDER BY {ordering}",
                )
                for table, ordering in (
                    ("stations", "station_id"),
                    ("connectors", "connector_id"),
                    ("station_status_intervals", "station_id, started_at"),
                    ("connector_status_intervals", "connector_id, started_at"),
                    ("collector_state", "singleton_id"),
                    ("collector_gaps", "started_at"),
                )
            }
            station_history = _rows_as_dicts(
                connection,
                """
                WITH state AS (
                    SELECT last_success_at
                    FROM collector_state
                    WHERE singleton_id = 1
                ),
                ordered AS (
                    SELECT
                        history.*,
                        stations.name AS station_name,
                        stations.address,
                        state.last_success_at,
                        lag(history.last_confirmed_at) OVER (
                            PARTITION BY history.station_id
                            ORDER BY history.started_at
                        ) AS previous_last_confirmed_at
                    FROM station_status_intervals AS history
                    JOIN stations USING (station_id)
                    CROSS JOIN state
                ),
                bounded AS (
                    SELECT
                        *,
                        CASE
                            WHEN previous_last_confirmed_at IS NULL
                                THEN started_at
                            ELSE previous_last_confirmed_at
                                + ((started_at - previous_last_confirmed_at) / 2)
                        END AS estimated_started_at,
                        CASE
                            WHEN ended_at IS NULL
                                THEN coalesce(last_success_at, last_confirmed_at)
                            ELSE last_confirmed_at
                                + ((ended_at - last_confirmed_at) / 2)
                        END AS estimated_ended_at,
                        CASE
                            WHEN ended_at IS NULL
                                THEN coalesce(last_success_at, last_confirmed_at)
                            ELSE last_confirmed_at
                        END AS effective_last_confirmed_at
                    FROM ordered
                )
                SELECT
                    station_name,
                    address,
                    station_id,
                    status,
                    started_at AS first_observed_at,
                    effective_last_confirmed_at AS last_confirmed_at,
                    ended_at AS next_status_observed_at,
                    estimated_started_at,
                    estimated_ended_at,
                    epoch(estimated_ended_at - estimated_started_at) / 3600
                        AS midpoint_estimated_hours,
                    round(
                        epoch(effective_last_confirmed_at - started_at) / 3600,
                        6
                    ) AS confirmed_hours_after_first_seen,
                    round(
                        epoch(
                            coalesce(ended_at, last_success_at, last_confirmed_at)
                            - started_at
                        ) / 3600,
                        6
                    ) AS possible_hours_until_next_observation,
                    round(
                        CASE
                            WHEN ended_at IS NULL THEN 0
                            ELSE epoch(ended_at - last_confirmed_at) / 60
                        END,
                        3
                    ) AS transition_uncertainty_minutes,
                    ended_at IS NULL AS is_ongoing
                FROM bounded
                ORDER BY first_observed_at DESC, station_name, station_id
                """,
            )
            connector_history = _rows_as_dicts(
                connection,
                """
                WITH state AS (
                    SELECT last_success_at
                    FROM collector_state
                    WHERE singleton_id = 1
                ),
                ordered AS (
                    SELECT
                        history.*,
                        stations.name AS station_name,
                        stations.address,
                        connectors.evse_name,
                        connectors.connector_number,
                        connectors.connector_type,
                        connectors.connector_type_group,
                        state.last_success_at,
                        lag(history.last_confirmed_at) OVER (
                            PARTITION BY history.connector_id
                            ORDER BY history.started_at
                        ) AS previous_last_confirmed_at
                    FROM connector_status_intervals AS history
                    JOIN stations USING (station_id)
                    LEFT JOIN connectors USING (connector_id)
                    CROSS JOIN state
                ),
                bounded AS (
                    SELECT
                        *,
                        CASE
                            WHEN previous_last_confirmed_at IS NULL
                                THEN started_at
                            ELSE previous_last_confirmed_at
                                + ((started_at - previous_last_confirmed_at) / 2)
                        END AS estimated_started_at,
                        CASE
                            WHEN ended_at IS NULL
                                THEN coalesce(last_success_at, last_confirmed_at)
                            ELSE last_confirmed_at
                                + ((ended_at - last_confirmed_at) / 2)
                        END AS estimated_ended_at,
                        CASE
                            WHEN ended_at IS NULL
                                THEN coalesce(last_success_at, last_confirmed_at)
                            ELSE last_confirmed_at
                        END AS effective_last_confirmed_at
                    FROM ordered
                )
                SELECT
                    station_name,
                    address,
                    station_id,
                    evse_name,
                    connector_number,
                    connector_type,
                    connector_type_group,
                    connector_id,
                    status_code,
                    status,
                    status_description,
                    power_kw,
                    price,
                    metadata_basis,
                    battery_at_start,
                    battery_at_end,
                    started_at AS first_observed_at,
                    effective_last_confirmed_at AS last_confirmed_at,
                    ended_at AS next_status_observed_at,
                    estimated_started_at,
                    estimated_ended_at,
                    epoch(estimated_ended_at - estimated_started_at) / 3600
                        AS midpoint_estimated_hours,
                    round(
                        epoch(effective_last_confirmed_at - started_at) / 3600,
                        6
                    ) AS confirmed_hours_after_first_seen,
                    round(
                        epoch(
                            coalesce(ended_at, last_success_at, last_confirmed_at)
                            - started_at
                        ) / 3600,
                        6
                    ) AS possible_hours_until_next_observation,
                    round(
                        CASE
                            WHEN ended_at IS NULL THEN 0
                            ELSE epoch(ended_at - last_confirmed_at) / 60
                        END,
                        3
                    ) AS transition_uncertainty_minutes,
                    ended_at IS NULL AS is_ongoing
                FROM bounded
                ORDER BY first_observed_at DESC, station_name, station_id, connector_id
                """,
            )

        station_metrics: dict[str, dict[str, Any]] = {}
        connectors_by_station: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for connector in raw_tables["connectors"]:
            connectors_by_station[connector["station_id"]].append(connector)

        for station in raw_tables["stations"]:
            station_id = station["station_id"]
            connectors = connectors_by_station.get(station_id, [])
            station_metrics[station_id] = {
                "station_name": station.get("name") or station_id,
                "address": station.get("address"),
                "current_status": station.get("current_status"),
                "connector_count": len(connectors),
                "current_available_connectors": sum(
                    connector.get("status") == "available"
                    for connector in connectors
                ),
                "observation_start": None,
                "observation_end": None,
                "available_hours": 0.0,
                "busy_hours": 0.0,
                "maintenance_hours": 0.0,
                "unknown_hours": 0.0,
                "observed_station_hours": 0.0,
                "known_station_hours": 0.0,
                "coverage_percent": 0.0,
                "busy_percent": 0.0,
                "availability_percent": 0.0,
                "maintenance_percent": 0.0,
                "busy_events": 0,
                "charging_events": 0,
                "charging_connector_hours": 0.0,
                "rated_energy_ceiling_kwh": 0.0,
                "energy_weighted_price": None,
                "rated_power_revenue_ceiling_amd": 0.0,
                "transition_uncertainty_minutes": 0.0,
                "station_id": station_id,
                "latitude": station.get("latitude"),
                "longitude": station.get("longitude"),
            }

        for interval in station_history:
            metric = station_metrics.get(interval["station_id"])
            if metric is None:
                continue
            status_key = f"{interval['status']}_hours"
            if status_key not in metric:
                status_key = "unknown_hours"
            duration = float(interval.get("midpoint_estimated_hours") or 0)
            metric[status_key] += duration
            if interval["status"] == "busy":
                metric["busy_events"] += 1
            metric["transition_uncertainty_minutes"] += float(
                interval.get("transition_uncertainty_minutes") or 0
            )
            estimated_start = interval.get("estimated_started_at")
            estimated_end = interval.get("estimated_ended_at")
            if estimated_start is not None and (
                metric["observation_start"] is None
                or estimated_start < metric["observation_start"]
            ):
                metric["observation_start"] = estimated_start
            if estimated_end is not None and (
                metric["observation_end"] is None
                or estimated_end > metric["observation_end"]
            ):
                metric["observation_end"] = estimated_end

        price_numerators: dict[str, float] = defaultdict(float)
        for interval in connector_history:
            if interval.get("status") != "charging":
                continue
            metric = station_metrics.get(interval["station_id"])
            if metric is None:
                continue
            duration = float(interval.get("midpoint_estimated_hours") or 0)
            power_kw = float(interval.get("power_kw") or 0)
            price = float(interval.get("price") or 0)
            rated_energy = duration * power_kw
            metric["charging_events"] += 1
            metric["charging_connector_hours"] += duration
            metric["rated_energy_ceiling_kwh"] += rated_energy
            metric["rated_power_revenue_ceiling_amd"] += rated_energy * price
            price_numerators[interval["station_id"]] += rated_energy * price

        connector_analytics = self._build_connector_analytics(
            raw_tables["connectors"],
            station_metrics,
            connector_history,
        )

        station_analytics = list(station_metrics.values())
        for metric in station_analytics:
            observed_hours = sum(
                float(metric[key])
                for key in (
                    "available_hours",
                    "busy_hours",
                    "maintenance_hours",
                    "unknown_hours",
                )
            )
            known_hours = observed_hours - float(metric["unknown_hours"])
            metric["observed_station_hours"] = observed_hours
            metric["known_station_hours"] = known_hours
            metric["coverage_percent"] = (
                known_hours / observed_hours if observed_hours else 0.0
            )
            metric["busy_percent"] = (
                float(metric["busy_hours"]) / known_hours if known_hours else 0.0
            )
            metric["availability_percent"] = (
                float(metric["available_hours"]) / known_hours
                if known_hours
                else 0.0
            )
            metric["maintenance_percent"] = (
                float(metric["maintenance_hours"]) / known_hours
                if known_hours
                else 0.0
            )
            energy = metric["rated_energy_ceiling_kwh"]
            if energy > 0:
                metric["energy_weighted_price"] = (
                    price_numerators[metric["station_id"]] / energy
                )
            for key in (
                "available_hours",
                "busy_hours",
                "maintenance_hours",
                "unknown_hours",
                "observed_station_hours",
                "known_station_hours",
                "coverage_percent",
                "busy_percent",
                "availability_percent",
                "maintenance_percent",
                "charging_connector_hours",
                "rated_energy_ceiling_kwh",
                "rated_power_revenue_ceiling_amd",
                "transition_uncertainty_minutes",
            ):
                metric[key] = round(float(metric[key]), 6)
            if metric["energy_weighted_price"] is not None:
                metric["energy_weighted_price"] = round(
                    float(metric["energy_weighted_price"]), 6
                )
            metric["scenario_load_factor"] = 0.5
            metric["scenario_energy_kwh"] = round(
                metric["rated_energy_ceiling_kwh"]
                * metric["scenario_load_factor"],
                6,
            )
            metric["scenario_revenue_amd"] = round(
                metric["rated_power_revenue_ceiling_amd"]
                * metric["scenario_load_factor"],
                6,
            )
        station_analytics.sort(
            key=lambda metric: (
                -metric["busy_hours"],
                -metric["maintenance_hours"],
                metric["station_name"],
            )
        )

        states = raw_tables["collector_state"]
        last_success_at = states[0].get("last_success_at") if states else None
        observation_starts = [
            metric["observation_start"]
            for metric in station_analytics
            if metric["observation_start"] is not None
        ]
        return {
            "generated_at": generated_at,
            "observation_start": min(observation_starts) if observation_starts else None,
            "observation_end": last_success_at,
            "assumptions": {
                "scenario_load_factor": 0.5,
                "currency": "AMD",
                "price_interpretation": "provider price field per kWh",
                "gap_threshold_seconds": self.gap_threshold_seconds,
            },
            "station_analytics": station_analytics,
            "connector_analytics": connector_analytics,
            "station_history": station_history,
            "connector_history": connector_history,
            "raw_tables": raw_tables,
        }

    def dashboard_analytics(
        self,
        hours: int | None = 24,
        grid_degrees: float = 0.025,
    ) -> dict[str, Any]:
        """Return live map metrics clipped to one requested observation window."""
        if hours is not None and hours < 1:
            hours = None
        if not 0.005 <= grid_degrees <= 1:
            raise ValueError("grid_degrees must be between 0.005 and 1")

        payload = self.analytics_payload()
        window_end = payload.get("observation_end")
        coverage_start = payload.get("observation_start")
        if window_end is None or coverage_start is None:
            return {
                "generated_at": payload["generated_at"],
                "window": {
                    "requested_hours": hours,
                    "start": None,
                    "end": None,
                    "coverage_hours": 0.0,
                },
                "assumptions": payload["assumptions"],
                "portfolio": {
                    "station_count": 0,
                    "current_available": 0,
                    "current_busy": 0,
                    "current_maintenance": 0,
                    "observed_station_hours": 0.0,
                    "known_station_hours": 0.0,
                    "unknown_station_hours": 0.0,
                    "coverage_percent": 0.0,
                    "busy_hours": 0.0,
                    "busy_percent": 0.0,
                    "maintenance_hours": 0.0,
                    "charging_connector_hours": 0.0,
                    "scenario_revenue_amd": 0.0,
                },
                "stations": [],
                "areas": [],
            }

        requested_start = (
            window_end - timedelta(hours=hours) if hours is not None else coverage_start
        )
        window_start = max(coverage_start, requested_start)
        scenario_load_factor = float(
            payload["assumptions"]["scenario_load_factor"]
        )
        connectors_by_station: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for connector in payload["raw_tables"]["connectors"]:
            connectors_by_station[connector["station_id"]].append(connector)

        metrics: dict[str, dict[str, Any]] = {}
        for station in payload["raw_tables"]["stations"]:
            station_id = station["station_id"]
            connectors = connectors_by_station.get(station_id, [])
            metrics[station_id] = {
                "station_id": station_id,
                "name": station.get("name") or station_id,
                "address": station.get("address"),
                "latitude": station.get("latitude"),
                "longitude": station.get("longitude"),
                "current_status": station.get("current_status"),
                "connector_count": len(connectors),
                "available_connectors": sum(
                    connector.get("status") == "available"
                    for connector in connectors
                ),
                "observed_hours": 0.0,
                "known_hours": 0.0,
                "available_hours": 0.0,
                "busy_hours": 0.0,
                "maintenance_hours": 0.0,
                "unknown_hours": 0.0,
                "coverage_percent": 0.0,
                "busy_percent": 0.0,
                "availability_percent": 0.0,
                "maintenance_percent": 0.0,
                "charging_connector_hours": 0.0,
                "rated_energy_ceiling_kwh": 0.0,
                "weighted_price_amd_per_kwh": None,
                "scenario_revenue_amd": 0.0,
                "busy_intervals": 0,
            }

        for interval in payload["station_history"]:
            metric = metrics.get(interval["station_id"])
            estimated_start = interval.get("estimated_started_at")
            estimated_end = interval.get("estimated_ended_at")
            if metric is None or estimated_start is None or estimated_end is None:
                continue
            clipped_start = max(estimated_start, window_start)
            clipped_end = min(estimated_end, window_end)
            if clipped_end <= clipped_start:
                continue
            duration = (clipped_end - clipped_start).total_seconds() / 3600
            status_key = f"{interval['status']}_hours"
            if status_key not in metric:
                status_key = "unknown_hours"
            metric[status_key] += duration
            metric["observed_hours"] += duration
            if interval["status"] == "busy":
                metric["busy_intervals"] += 1

        price_numerators: dict[str, float] = defaultdict(float)
        for interval in payload["connector_history"]:
            if interval.get("status") != "charging":
                continue
            metric = metrics.get(interval["station_id"])
            estimated_start = interval.get("estimated_started_at")
            estimated_end = interval.get("estimated_ended_at")
            if metric is None or estimated_start is None or estimated_end is None:
                continue
            clipped_start = max(estimated_start, window_start)
            clipped_end = min(estimated_end, window_end)
            if clipped_end <= clipped_start:
                continue
            duration = (clipped_end - clipped_start).total_seconds() / 3600
            power_kw = float(interval.get("power_kw") or 0)
            price = float(interval.get("price") or 0)
            rated_energy = duration * power_kw
            metric["charging_connector_hours"] += duration
            metric["rated_energy_ceiling_kwh"] += rated_energy
            price_numerators[interval["station_id"]] += rated_energy * price

        station_rows = list(metrics.values())
        for metric in station_rows:
            energy = metric["rated_energy_ceiling_kwh"]
            if energy > 0:
                metric["weighted_price_amd_per_kwh"] = (
                    price_numerators[metric["station_id"]] / energy
                )
            if metric["observed_hours"] > 0:
                metric["known_hours"] = (
                    metric["observed_hours"] - metric["unknown_hours"]
                )
                metric["coverage_percent"] = (
                    metric["known_hours"] / metric["observed_hours"]
                )
            if metric["known_hours"] > 0:
                metric["busy_percent"] = metric["busy_hours"] / metric["known_hours"]
                metric["availability_percent"] = (
                    metric["available_hours"] / metric["known_hours"]
                )
                metric["maintenance_percent"] = (
                    metric["maintenance_hours"] / metric["known_hours"]
                )
            metric["scenario_revenue_amd"] = (
                price_numerators[metric["station_id"]] * scenario_load_factor
            )
        station_rows.sort(key=lambda row: (-row["busy_hours"], row["name"]))

        area_metrics: dict[tuple[int, int], dict[str, Any]] = {}
        for station in station_rows:
            latitude = station.get("latitude")
            longitude = station.get("longitude")
            if latitude is None or longitude is None:
                continue
            lat_index = math.floor(float(latitude) / grid_degrees)
            lon_index = math.floor(float(longitude) / grid_degrees)
            key = (lat_index, lon_index)
            area = area_metrics.setdefault(
                key,
                {
                    "south": lat_index * grid_degrees,
                    "west": lon_index * grid_degrees,
                    "north": (lat_index + 1) * grid_degrees,
                    "east": (lon_index + 1) * grid_degrees,
                    "station_count": 0,
                    "current_busy_stations": 0,
                    "observed_station_hours": 0.0,
                    "known_station_hours": 0.0,
                    "unknown_station_hours": 0.0,
                    "coverage_percent": 0.0,
                    "busy_hours": 0.0,
                    "charging_connector_hours": 0.0,
                    "scenario_revenue_amd": 0.0,
                    "top_station_name": None,
                    "top_station_busy_hours": -1.0,
                },
            )
            area["station_count"] += 1
            area["current_busy_stations"] += station["current_status"] == "busy"
            area["observed_station_hours"] += station["observed_hours"]
            area["known_station_hours"] += station["known_hours"]
            area["unknown_station_hours"] += station["unknown_hours"]
            area["busy_hours"] += station["busy_hours"]
            area["charging_connector_hours"] += station[
                "charging_connector_hours"
            ]
            area["scenario_revenue_amd"] += station["scenario_revenue_amd"]
            if station["busy_hours"] > area["top_station_busy_hours"]:
                area["top_station_name"] = station["name"]
                area["top_station_busy_hours"] = station["busy_hours"]

        areas = list(area_metrics.values())
        for area in areas:
            observed = area["observed_station_hours"]
            known = area["known_station_hours"]
            area["coverage_percent"] = known / observed if observed else 0.0
            area["busy_percent"] = area["busy_hours"] / known if known else 0.0
            area["center_latitude"] = (area["south"] + area["north"]) / 2
            area["center_longitude"] = (area["west"] + area["east"]) / 2
            area.pop("top_station_busy_hours", None)
            for key in (
                "south",
                "west",
                "north",
                "east",
                "center_latitude",
                "center_longitude",
                "observed_station_hours",
                "known_station_hours",
                "unknown_station_hours",
                "coverage_percent",
                "busy_hours",
                "busy_percent",
                "charging_connector_hours",
                "scenario_revenue_amd",
            ):
                area[key] = round(float(area[key]), 6)
        areas.sort(key=lambda area: -area["busy_hours"])

        observed_hours = sum(row["observed_hours"] for row in station_rows)
        known_hours = sum(row["known_hours"] for row in station_rows)
        unknown_hours = sum(row["unknown_hours"] for row in station_rows)
        busy_hours = sum(row["busy_hours"] for row in station_rows)
        portfolio = {
            "station_count": len(station_rows),
            "current_available": sum(
                row["current_status"] == "available" for row in station_rows
            ),
            "current_busy": sum(
                row["current_status"] == "busy" for row in station_rows
            ),
            "current_maintenance": sum(
                row["current_status"] == "maintenance" for row in station_rows
            ),
            "observed_station_hours": round(observed_hours, 6),
            "known_station_hours": round(known_hours, 6),
            "unknown_station_hours": round(unknown_hours, 6),
            "coverage_percent": round(
                known_hours / observed_hours if observed_hours else 0.0,
                6,
            ),
            "busy_hours": round(busy_hours, 6),
            "busy_percent": round(
                busy_hours / known_hours if known_hours else 0.0,
                6,
            ),
            "maintenance_hours": round(
                sum(row["maintenance_hours"] for row in station_rows), 6
            ),
            "charging_connector_hours": round(
                sum(row["charging_connector_hours"] for row in station_rows), 6
            ),
            "scenario_revenue_amd": round(
                sum(row["scenario_revenue_amd"] for row in station_rows), 6
            ),
        }
        for metric in station_rows:
            for key in (
                "observed_hours",
                "known_hours",
                "available_hours",
                "busy_hours",
                "maintenance_hours",
                "unknown_hours",
                "coverage_percent",
                "busy_percent",
                "availability_percent",
                "maintenance_percent",
                "charging_connector_hours",
                "rated_energy_ceiling_kwh",
                "scenario_revenue_amd",
            ):
                metric[key] = round(float(metric[key]), 6)
            if metric["weighted_price_amd_per_kwh"] is not None:
                metric["weighted_price_amd_per_kwh"] = round(
                    float(metric["weighted_price_amd_per_kwh"]), 6
                )
        return {
            "generated_at": payload["generated_at"],
            "window": {
                "requested_hours": hours,
                "start": window_start,
                "end": window_end,
                "coverage_hours": round(
                    (window_end - window_start).total_seconds() / 3600,
                    6,
                ),
            },
            "assumptions": payload["assumptions"],
            "portfolio": portfolio,
            "stations": station_rows,
            "areas": areas,
        }

    @staticmethod
    def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
        output = io.StringIO(newline="")
        if rows:
            writer = csv.DictWriter(output, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return ("\ufeff" + output.getvalue()).encode("utf-8")

    def export_analytics_archive(self) -> tuple[str, bytes, dict[str, int]]:
        """Return a decision workbook plus readable and raw CSV source tables."""
        payload = self.analytics_payload()
        workbook_content = build_analytics_workbook(payload)
        archive_buffer = io.BytesIO()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        row_counts = {
            "station_analytics": len(payload["station_analytics"]),
            "connector_analytics": len(payload["connector_analytics"]),
            "station_history": len(payload["station_history"]),
            "connector_history": len(payload["connector_history"]),
            **{
                table: len(rows)
                for table, rows in payload["raw_tables"].items()
            },
        }
        workbook_name = f"team_energy_analytics_{timestamp}.xlsx"
        with zipfile.ZipFile(
            archive_buffer,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            archive.writestr(workbook_name, workbook_content)
            archive.writestr(
                "station_analytics.csv",
                self._csv_bytes(payload["station_analytics"]),
            )
            archive.writestr(
                "connector_analytics.csv",
                self._csv_bytes(payload["connector_analytics"]),
            )
            archive.writestr(
                "readable_station_history.csv",
                self._csv_bytes(payload["station_history"]),
            )
            archive.writestr(
                "readable_connector_history.csv",
                self._csv_bytes(payload["connector_history"]),
            )
            for filename, (table, _) in EXPORT_TABLES.items():
                archive.writestr(
                    filename,
                    self._csv_bytes(payload["raw_tables"][table]),
                )
            manifest = [
                "Team Energy analytics and raw-data export",
                f"Generated at: {payload['generated_at'].isoformat()}",
                "",
                "Start with the Excel workbook for the dashboard, station names,",
                "readable intervals, formulas, methodology, and raw-data sheets.",
                "The CSV files are retained for programmatic use and auditability.",
                "",
                "Important: revenue is a scenario estimate, not actual revenue.",
                "It uses charging-status time, rated connector power, the provider",
                "price field, and an editable 50% load-factor assumption.",
                "",
                "Rows:",
            ]
            manifest.extend(
                f"- {table}: {count}"
                for table, count in sorted(row_counts.items())
            )
            archive.writestr("README.txt", "\n".join(manifest) + "\n")
        return (
            f"team_energy_analytics_and_raw_{timestamp}.zip",
            archive_buffer.getvalue(),
            row_counts,
        )

    def export_csv_archive(self) -> tuple[str, bytes, dict[str, int]]:
        """Return all DuckDB tables as Excel-compatible UTF-8 CSV files."""
        archive_buffer = io.BytesIO()
        row_counts: dict[str, int] = {}
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        with self._lock, self._connect() as connection:
            with zipfile.ZipFile(
                archive_buffer,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                for filename, (table, ordering) in EXPORT_TABLES.items():
                    rows = _rows_as_dicts(
                        connection,
                        f"SELECT * FROM {table} ORDER BY {ordering}",
                    )
                    row_counts[table] = len(rows)
                    output = io.StringIO(newline="")
                    if rows:
                        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
                        writer.writeheader()
                        writer.writerows(rows)
                    archive.writestr(filename, "\ufeff" + output.getvalue())

                manifest = [
                    "Team Energy DuckDB CSV export",
                    f"Generated at: {datetime.now(timezone.utc).isoformat()}",
                    "Encoding: UTF-8 with BOM (Excel compatible)",
                    "",
                    "Rows:",
                ]
                manifest.extend(
                    f"- {table}: {count}"
                    for table, count in sorted(row_counts.items())
                )
                archive.writestr("README.txt", "\n".join(manifest) + "\n")

        return (
            f"team_energy_all_data_{timestamp}.zip",
            archive_buffer.getvalue(),
            row_counts,
        )
