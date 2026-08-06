"""One-time, auditable repair for a known Team Energy collector outage.

The application code is intentionally untouched.  This tool splits every
station and connector status interval that crosses the supplied outage and
inserts a synthetic ``unknown`` interval.  It is deliberately opt-in: use
``--apply`` only after creating a verified database backup.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb


GAP_ID = "collector-offline-2026-08-06-battery"
# The final confirmed event occurred at 02:25:16.186071. Start the synthetic
# gap one microsecond later so that real observation remains intact.
GAP_START = datetime.fromisoformat("2026-08-06T02:25:16.186072+04:00")
GAP_END = datetime.fromisoformat("2026-08-06T10:53:04.944975+04:00")
GAP_REASON = "Collector offline: MacBook battery depleted"


@dataclass(frozen=True)
class TableSpec:
    table: str
    entity_column: str
    columns: tuple[str, ...]


STATION_SPEC = TableSpec(
    table="station_status_intervals",
    entity_column="station_id",
    columns=("station_id", "status", "started_at", "last_confirmed_at", "ended_at"),
)
CONNECTOR_SPEC = TableSpec(
    table="connector_status_intervals",
    entity_column="connector_id",
    columns=(
        "connector_id",
        "station_id",
        "status_code",
        "status",
        "status_description",
        "power_kw",
        "price",
        "metadata_basis",
        "started_at",
        "last_confirmed_at",
        "ended_at",
    ),
)


def rows_as_dicts(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    parameters: list[Any] | None = None,
) -> list[dict[str, Any]]:
    cursor = connection.execute(sql, parameters or [])
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def scalar(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    parameters: list[Any] | None = None,
) -> int:
    return int(connection.execute(sql, parameters or []).fetchone()[0])


def active_through_gap(
    connection: duckdb.DuckDBPyConnection,
    spec: TableSpec,
) -> list[dict[str, Any]]:
    return rows_as_dicts(
        connection,
        f"""
        SELECT {", ".join(spec.columns)}
        FROM {spec.table}
        WHERE started_at < ?
          AND (ended_at IS NULL OR ended_at >= ?)
        ORDER BY {spec.entity_column}, started_at
        """,
        [GAP_START, GAP_END],
    )


def validate_preconditions(
    connection: duckdb.DuckDBPyConnection,
    spec: TableSpec,
) -> list[dict[str, Any]]:
    rows = active_through_gap(connection, spec)
    if not rows:
        raise ValueError(f"No {spec.table} rows span the requested gap")

    boundaries_inside_gap = scalar(
        connection,
        f"""
        SELECT count(*)
        FROM {spec.table}
        WHERE (started_at > ? AND started_at < ?)
           OR (ended_at > ? AND ended_at < ?)
        """,
        [GAP_START, GAP_END, GAP_START, GAP_END],
    )
    if boundaries_inside_gap:
        raise ValueError(
            f"{spec.table} has {boundaries_inside_gap} interval boundaries "
            "inside the requested gap; aborting rather than guessing."
        )

    start_conflicts = scalar(
        connection,
        f"SELECT count(*) FROM {spec.table} WHERE started_at = ?",
        [GAP_START],
    )
    if start_conflicts:
        raise ValueError(
            f"{spec.table} already has {start_conflicts} rows at the gap start"
        )

    for row in rows:
        ended_at = row["ended_at"]
        if ended_at == GAP_END:
            next_count = scalar(
                connection,
                f"""
                SELECT count(*) FROM {spec.table}
                WHERE {spec.entity_column} = ? AND started_at = ?
                """,
                [row[spec.entity_column], GAP_END],
            )
            if next_count != 1:
                raise ValueError(
                    f"{spec.table} has no unique post-gap interval for "
                    f"{row[spec.entity_column]}"
                )
    return rows


def insert_unknown(
    connection: duckdb.DuckDBPyConnection,
    spec: TableSpec,
    row: dict[str, Any],
) -> None:
    if spec is STATION_SPEC:
        connection.execute(
            """
            INSERT INTO station_status_intervals (
                station_id, status, started_at, last_confirmed_at, ended_at
            ) VALUES (?, 'unknown', ?, ?, ?)
            """,
            [row["station_id"], GAP_START, GAP_END, GAP_END],
        )
        return

    connection.execute(
        """
        INSERT INTO connector_status_intervals (
            connector_id, station_id, status_code, status, status_description,
            power_kw, price, metadata_basis, started_at, last_confirmed_at, ended_at
        ) VALUES (?, ?, 'collector_offline', 'unknown', ?, NULL, NULL,
                  'manual_collector_gap', ?, ?, ?)
        """,
        [
            row["connector_id"],
            row["station_id"],
            GAP_REASON,
            GAP_START,
            GAP_END,
            GAP_END,
        ],
    )


def insert_resumed_interval(
    connection: duckdb.DuckDBPyConnection,
    spec: TableSpec,
    row: dict[str, Any],
) -> None:
    ended_at = row["ended_at"]
    if ended_at == GAP_END:
        return

    resumed = dict(row)
    resumed["started_at"] = GAP_END
    resumed["last_confirmed_at"] = max(row["last_confirmed_at"], GAP_END)
    placeholders = ", ".join("?" for _ in spec.columns)
    connection.execute(
        f"""
        INSERT INTO {spec.table} ({", ".join(spec.columns)})
        VALUES ({placeholders})
        """,
        [resumed[column] for column in spec.columns],
    )


def split_for_gap(
    connection: duckdb.DuckDBPyConnection,
    spec: TableSpec,
    rows: list[dict[str, Any]],
) -> None:
    for row in rows:
        connection.execute(
            f"""
            UPDATE {spec.table}
            SET last_confirmed_at = ?, ended_at = ?
            WHERE {spec.entity_column} = ? AND started_at = ?
            """,
            [GAP_START, GAP_START, row[spec.entity_column], row["started_at"]],
        )
        insert_unknown(connection, spec, row)
        insert_resumed_interval(connection, spec, row)


def create_audit_table(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS manual_collector_gaps (
            gap_id VARCHAR PRIMARY KEY,
            started_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ NOT NULL,
            reason VARCHAR NOT NULL,
            repaired_at TIMESTAMPTZ NOT NULL,
            station_intervals_marked BIGINT NOT NULL,
            connector_intervals_marked BIGINT NOT NULL
        )
        """
    )


def validate_result(
    connection: duckdb.DuckDBPyConnection,
    spec: TableSpec,
    expected_unknown: int,
) -> None:
    unknown_count = scalar(
        connection,
        f"""
        SELECT count(*)
        FROM {spec.table}
        WHERE status = 'unknown' AND started_at = ? AND ended_at = ?
        """,
        [GAP_START, GAP_END],
    )
    if unknown_count != expected_unknown:
        raise ValueError(
            f"Expected {expected_unknown} unknown intervals in {spec.table}; "
            f"found {unknown_count}"
        )

    remaining_crossing = scalar(
        connection,
        f"""
        SELECT count(*)
        FROM {spec.table}
        WHERE status <> 'unknown'
          AND started_at < ?
          AND (ended_at IS NULL OR ended_at > ?)
        """,
        [GAP_START, GAP_END],
    )
    if remaining_crossing:
        raise ValueError(
            f"{spec.table} still has {remaining_crossing} non-unknown rows "
            "crossing the repaired gap"
        )

    overlap_count = scalar(
        connection,
        f"""
        WITH ordered AS (
            SELECT
                {spec.entity_column},
                started_at,
                lag(ended_at) OVER (
                    PARTITION BY {spec.entity_column}
                    ORDER BY started_at
                ) AS previous_ended_at
            FROM {spec.table}
        )
        SELECT count(*)
        FROM ordered
        WHERE previous_ended_at > started_at
        """,
    )
    if overlap_count:
        raise ValueError(
            f"{spec.table} has {overlap_count} overlapping intervals after repair"
        )


def repair(database_path: Path, apply: bool) -> dict[str, int]:
    with duckdb.connect(str(database_path)) as connection:
        station_rows = validate_preconditions(connection, STATION_SPEC)
        connector_rows = validate_preconditions(connection, CONNECTOR_SPEC)
        summary = {
            "station_intervals_marked": len(station_rows),
            "connector_intervals_marked": len(connector_rows),
        }
        if not apply:
            return summary

        connection.execute("BEGIN TRANSACTION")
        try:
            create_audit_table(connection)
            prior = scalar(
                connection,
                "SELECT count(*) FROM manual_collector_gaps WHERE gap_id = ?",
                [GAP_ID],
            )
            if prior:
                raise ValueError(f"Gap {GAP_ID} is already recorded")
            split_for_gap(connection, STATION_SPEC, station_rows)
            split_for_gap(connection, CONNECTOR_SPEC, connector_rows)
            connection.execute(
                """
                INSERT INTO manual_collector_gaps (
                    gap_id, started_at, ended_at, reason, repaired_at,
                    station_intervals_marked, connector_intervals_marked
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    GAP_ID,
                    GAP_START,
                    GAP_END,
                    GAP_REASON,
                    datetime.now(timezone.utc),
                    summary["station_intervals_marked"],
                    summary["connector_intervals_marked"],
                ],
            )
            validate_result(
                connection,
                STATION_SPEC,
                summary["station_intervals_marked"],
            )
            validate_result(
                connection,
                CONNECTOR_SPEC,
                summary["connector_intervals_marked"],
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the repair. Omit this flag for a read-only validation run.",
    )
    args = parser.parse_args()
    if not args.database.is_file():
        raise FileNotFoundError(args.database)
    summary = repair(args.database, args.apply)
    mode = "APPLIED" if args.apply else "VALIDATED"
    print(
        f"{mode}: {GAP_START.isoformat()} -> {GAP_END.isoformat()} | "
        f"stations={summary['station_intervals_marked']} "
        f"connectors={summary['connector_intervals_marked']}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ABORTED: {error}", file=sys.stderr)
        raise SystemExit(1)
