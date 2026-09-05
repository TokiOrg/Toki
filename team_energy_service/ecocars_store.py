"""Storage for EcoCars network snapshots, with real session tracking.

Same append-only JSON-lines design as evan_store.py, but with proper
session-interval detection built in from the start (walking the poll log in
time order and detecting status transitions), rather than a poll-count
estimate. This is the lesson learned from the Evan integration.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .ecocars_provider import CONNECTOR_TYPE_NAMES, STATUS_NAMES


class EcoCarsStore:
    """Append-only JSON-lines store for raw EcoCars connector polls."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def record_snapshot(self, stations: list[dict[str, Any]], polled_at: datetime) -> int:
        """Append one line with every connector's status from this poll.

        Field mapping per the documented EcoCars response shape:
            station: id, name, address, geo.lat/lon, online, isInUse
            connector: id, type (int enum), status (int enum), maxOutputKw,
                       chargePercentage (string, "0" = no reading, same
                       0-means-unknown pattern seen on Team Energy and Evan),
                       rates.energy.price, rates.parking.price
        """
        connectors = []
        for station in stations:
            station_id = str(station.get("id") or "")
            if not station_id:
                continue
            station_name = station.get("name")
            address = station.get("address")
            geo = station.get("geo") or {}
            for connector in station.get("connectors") or []:
                connector_id = f"{station_id}:{connector.get('id')}"
                if connector.get("id") is None:
                    continue
                type_id = connector.get("type")
                status_id = connector.get("status")
                rates = connector.get("rates") or {}
                energy_rate = rates.get("energy") or {}
                parking_rate = rates.get("parking") or {}
                battery_raw = connector.get("chargePercentage")
                try:
                    battery = float(battery_raw) if battery_raw is not None else None
                except (TypeError, ValueError):
                    battery = None
                connectors.append(
                    {
                        "station_id": station_id,
                        "station_name": station_name,
                        "address": address,
                        "latitude": geo.get("lat"),
                        "longitude": geo.get("lon"),
                        "connector_id": connector_id,
                        "connector_type": CONNECTOR_TYPE_NAMES.get(
                            type_id, f"type_{type_id}"
                        ),
                        "power_kw": connector.get("maxOutputKw"),
                        "status": STATUS_NAMES.get(status_id, f"status_{status_id}"),
                        "active_charge_percent": battery,
                        "price": energy_rate.get("price"),
                        "parking_price_per_min": parking_rate.get("price"),
                    }
                )
        if not connectors:
            return 0
        line = json.dumps(
            {"polled_at": polled_at.isoformat(), "connectors": connectors},
            ensure_ascii=False,
        )
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return len(connectors)

    def _iter_polls_since(self, cutoff: datetime, until: datetime | None = None):
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                    polled_at = datetime.fromisoformat(record["polled_at"])
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
                if polled_at >= cutoff and (until is None or polled_at < until):
                    yield polled_at, record["connectors"]

    def sessions_last_hours(
        self, hours: int = 24, include_sessions: bool = False
    ) -> dict[str, Any]:
        """Reconstruct real charging sessions from the poll log, for the
        last N hours. See _sessions_for_window() for the core logic.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=hours)
        result = self._sessions_for_window(cutoff, now, include_sessions)
        return {"window_hours": hours, **result}

    def daily_sessions_summary(
        self, start_date: str, end_date: str, local_timezone: str = "Asia/Yerevan"
    ) -> dict[str, Any]:
        """Same metrics as sessions_last_hours(), but returned as one block
        per calendar day between start_date and end_date (inclusive), in
        local_timezone rather than one flat total for the whole range.

        start_date / end_date are "YYYY-MM-DD" strings. Works on any
        historical range already present in the poll log - only re-reads
        and re-buckets existing data. Note: only covers whatever history is
        actually in the poll log file - if ECOCARS_POLLS_PATH was not
        pointed at the persistent volume until partway through the
        requested range, earlier days may show zero/partial data.
        """
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(local_timezone)
        except Exception:  # pragma: no cover - fallback if tzdata missing
            tz = timezone.utc

        start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=tz)
        end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=tz)
        if end < start:
            raise ValueError("end_date must not be before start_date")

        days: list[dict[str, Any]] = []
        cursor = start
        while cursor <= end:
            day_start = cursor.astimezone(timezone.utc)
            day_end = (cursor + timedelta(days=1)).astimezone(timezone.utc)
            result = self._sessions_for_window(
                day_start, day_end, include_sessions=False
            )
            days.append({"date": cursor.strftime("%Y-%m-%d"), **result})
            cursor += timedelta(days=1)

        return {"start_date": start_date, "end_date": end_date, "days": days}

    def _sessions_for_window(
        self, window_start: datetime, window_end: datetime, include_sessions: bool
    ) -> dict[str, Any]:
        """Core session-reconstruction logic shared by sessions_last_hours()
        and daily_sessions_summary(). Same transition-detection approach
        validated on evan_store.py: walks each connector's polls in
        [window_start, window_end) in time order, detects "not charging ->
        charging -> not charging" as one session, and estimates duration via
        the midpoint of each transition window rather than claiming an
        exact second.
        """
        per_connector: dict[str, list[tuple[datetime, dict]]] = defaultdict(list)
        all_polls_in_window: list[datetime] = []
        for polled_at, connectors in self._iter_polls_since(
            window_start, window_end
        ):
            all_polls_in_window.append(polled_at)
            for connector in connectors:
                per_connector[connector["connector_id"]].append((polled_at, connector))

        all_polls_in_window.sort()
        if len(all_polls_in_window) >= 2:
            gap_seconds = (
                all_polls_in_window[-1] - all_polls_in_window[0]
            ).total_seconds() / (len(all_polls_in_window) - 1)
        else:
            gap_seconds = 30.0

        sessions: list[dict[str, Any]] = []
        for cid, rows in per_connector.items():
            rows.sort(key=lambda r: r[0])
            meta = rows[0][1]
            in_session = False
            session_start_poll: datetime | None = None
            session_prev_poll: datetime | None = None
            session_first_battery: float | None = None
            session_last_battery: float | None = None
            last_row_time: datetime | None = None

            def close_session(end_boundary_poll: datetime | None):
                if session_start_poll is None:
                    return
                est_start = session_start_poll
                if session_prev_poll is not None:
                    est_start = session_prev_poll + (
                        session_start_poll - session_prev_poll
                    ) / 2
                est_end = last_row_time
                if end_boundary_poll is not None and last_row_time is not None:
                    est_end = last_row_time + (
                        end_boundary_poll - last_row_time
                    ) / 2
                duration_hours = 0.0
                if est_end is not None and est_start is not None:
                    duration_hours = max(
                        0.0, (est_end - est_start).total_seconds() / 3600
                    )
                sessions.append(
                    {
                        "connector_id": cid,
                        "station_name": meta.get("station_name"),
                        "address": meta.get("address"),
                        "connector_type": meta.get("connector_type"),
                        "power_kw": meta.get("power_kw"),
                        "price": meta.get("price"),
                        "estimated_start": est_start.isoformat() if est_start else None,
                        "estimated_end": est_end.isoformat() if est_end else None,
                        "duration_hours": round(duration_hours, 3),
                        "battery_in_percent": session_first_battery,
                        "battery_out_percent": session_last_battery,
                    }
                )

            previous_poll_time: datetime | None = None
            for poll_time, row in rows:
                status = (row.get("status") or "").lower()
                is_charging = status == "charging"
                if is_charging and not in_session:
                    in_session = True
                    session_start_poll = poll_time
                    session_prev_poll = previous_poll_time
                    session_first_battery = None
                    session_last_battery = None
                if is_charging:
                    battery = row.get("active_charge_percent")
                    if battery is not None and battery > 0:
                        if session_first_battery is None:
                            session_first_battery = float(battery)
                        session_last_battery = float(battery)
                    last_row_time = poll_time
                elif in_session:
                    close_session(poll_time)
                    in_session = False
                    session_start_poll = None
                previous_poll_time = poll_time

            if in_session:
                close_session(None)

        total_sessions = len(sessions)
        total_hours = sum(s["duration_hours"] for s in sessions)

        by_station: dict[tuple, dict[str, Any]] = {}
        for s in sessions:
            key = (s["station_name"], s["address"])
            entry = by_station.setdefault(
                key,
                {
                    "station_name": s["station_name"],
                    "address": s["address"],
                    "cars_served": 0,
                    "hours": 0.0,
                },
            )
            entry["cars_served"] += 1
            entry["hours"] += s["duration_hours"]
        for entry in by_station.values():
            entry["hours"] = round(entry["hours"], 2)

        top_by_hours = sorted(by_station.values(), key=lambda e: -e["hours"])[:5]
        top_by_cars = sorted(by_station.values(), key=lambda e: -e["cars_served"])[:5]

        # AC/DC split by rated power (same >=50kW threshold used for Evan,
        # since EcoCars' data doesn't label station type at all).
        ac_hours = sum(s["duration_hours"] for s in sessions if (s["power_kw"] or 0) < 50)
        dc_hours = sum(s["duration_hours"] for s in sessions if (s["power_kw"] or 0) >= 50)
        ac_tiers: dict[float, float] = defaultdict(float)
        dc_tiers: dict[float, float] = defaultdict(float)
        for s in sessions:
            power = s["power_kw"] or 0
            if power < 50:
                ac_tiers[power] += s["duration_hours"]
            else:
                dc_tiers[power] += s["duration_hours"]

        LOAD_FACTOR = 0.5
        revenue = sum(
            s["duration_hours"] * (s["power_kw"] or 0) * LOAD_FACTOR * (s["price"] or 0)
            for s in sessions
        )

        battery_ins = [s["battery_in_percent"] for s in sessions if s["battery_in_percent"]]
        battery_outs = [s["battery_out_percent"] for s in sessions if s["battery_out_percent"]]
        battery_deltas = [
            s["battery_out_percent"] - s["battery_in_percent"]
            for s in sessions
            if s["battery_in_percent"] and s["battery_out_percent"]
        ]

        return {
            "approx_poll_interval_seconds": round(gap_seconds, 1),
            "total_sessions": total_sessions,
            "total_charging_hours": round(total_hours, 2),
            "ac_hours": round(ac_hours, 2),
            "dc_hours": round(dc_hours, 2),
            "ac_hours_by_power_kw": dict(sorted(ac_tiers.items())),
            "dc_hours_by_power_kw": dict(sorted(dc_tiers.items())),
            "scenario_revenue_amd_load_0_5": round(revenue, 0),
            "scenario_revenue_amd_load_0_29": round(revenue * 0.29 / 0.5, 0),
            "avg_battery_in_percent": round(sum(battery_ins) / len(battery_ins), 1) if battery_ins else None,
            "avg_battery_out_percent": round(sum(battery_outs) / len(battery_outs), 1) if battery_outs else None,
            "avg_battery_delta_percent": round(sum(battery_deltas) / len(battery_deltas), 1) if battery_deltas else None,
            "top_stations_by_hours": top_by_hours,
            "top_stations_by_cars": top_by_cars,
            "sessions": sessions if include_sessions else None,
        }