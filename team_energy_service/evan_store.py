"""Minimal standalone storage for Evan network snapshots.

Deliberately simple and separate from team_energy_service/database.py: no
new database engine, just one append-only JSON-lines file. Every poll writes
one line containing every connector's status at that moment. That's enough
to compute "how many hours was X charging in the last 24h" style summaries
on demand, without any of the interval-compression machinery the Team Energy
side uses.

File format: one JSON object per line, e.g.
    {"polled_at": "2026-08-16T10:00:00+00:00", "connectors": [
        {"station_id": "...", "station_name": "...", "address": "...",
         "connector_id": "...", "connector_type": "...", "power_kw": 22,
         "status": "charging"},
        ...
    ]}
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class EvanStore:
    """Append-only JSON-lines store for raw Evan connector polls."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def record_snapshot(self, stations: list[dict[str, Any]], polled_at: datetime) -> int:
        """Append one line with every connector's status from this poll.

        `stations` is the raw `data.stations` list from Evan's
        /api/stations/stations response. Confirmed real shape (from a
        captured sample):
            station: id, name, status, latitude, longitude,
                     stationModel.type ("ac"/"dc"),
                     stationAddress.address / .city
                     plugs: [ ... ]
            plug: id, connectorId, status ("Available"/"Charging"/"Faulted"),
                  activeChargePercent (0/null - same "0 = no reading yet"
                  pattern as Team Energy's stateOfBattery),
                  plugType.type (e.g. "CCS2", "GB/T DC", "Type 1"),
                  plugType.powerType ("ac"/"dc"),
                  plugTypeVariant.power (rated kW),
                  tariff.components: [{"type": "Charge", "price": ...}, ...]
                  (Evan's real per-kWh price - observed 100 or 120 AMD,
                  varying by station, unlike Team Energy's flat 120).
        """
        connectors = []
        for station in stations:
            station_id = str(station.get("id") or "")
            if not station_id:
                continue
            station_name = station.get("name")
            station_address_block = station.get("stationAddress") or {}
            address = station_address_block.get("address")
            city = station_address_block.get("city")
            for plug in station.get("plugs") or []:
                connector_id = str(plug.get("id") or "")
                if not connector_id:
                    continue
                plug_type = plug.get("plugType") or {}
                plug_variant = plug.get("plugTypeVariant") or {}
                tariff = plug.get("tariff") or {}
                price = None
                for component in tariff.get("components") or []:
                    if component.get("type") == "Charge":
                        price = component.get("price")
                        break
                connectors.append(
                    {
                        "station_id": station_id,
                        "station_name": station_name,
                        "address": address,
                        "city": city,
                        "connector_id": connector_id,
                        "connector_type": plug_type.get("type"),
                        "connector_type_group": (
                            plug_type.get("powerType") or ""
                        ).upper()
                        or None,
                        "power_kw": plug_variant.get("power"),
                        "status": plug.get("status"),
                        "active_charge_percent": plug.get("activeChargePercent"),
                        "price": price,
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

    def _iter_polls_since(self, cutoff: datetime):
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
                if polled_at >= cutoff:
                    yield polled_at, record["connectors"]

    def summary_last_hours(self, hours: int = 24) -> dict[str, Any]:
        """Rough usage summary from raw polls over the last N hours.

        Poll-count based: each poll represents roughly one polling interval
        of time in whatever status it shows. Good enough for "how many
        hours was X charging" without interval-compression engineering.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        poll_times: list[datetime] = []
        status_counts: dict[str, int] = defaultdict(int)
        connector_charging_counts: dict[str, int] = defaultdict(int)
        connector_meta: dict[str, dict[str, Any]] = {}

        for polled_at, connectors in self._iter_polls_since(cutoff):
            poll_times.append(polled_at)
            for connector in connectors:
                status = (connector.get("status") or "unknown").lower()
                status_counts[status] += 1
                cid = connector["connector_id"]
                connector_meta.setdefault(
                    cid,
                    {
                        "station_name": connector.get("station_name"),
                        "address": connector.get("address"),
                        "connector_type": connector.get("connector_type"),
                        "power_kw": connector.get("power_kw"),
                    },
                )
                if "charg" in status:
                    connector_charging_counts[cid] += 1

        poll_times.sort()
        if len(poll_times) >= 2:
            span_seconds = (poll_times[-1] - poll_times[0]).total_seconds()
            gap_seconds = span_seconds / (len(poll_times) - 1)
        else:
            gap_seconds = 30.0  # fallback assumption, matches Team Energy's interval

        by_status = [
            {
                "status": status,
                "poll_rows": count,
                "approx_hours": round(count * gap_seconds / 3600, 2),
            }
            for status, count in sorted(
                status_counts.items(), key=lambda item: -item[1]
            )
        ]
        by_connector = [
            {
                "connector_id": cid,
                **connector_meta[cid],
                "charging_polls": count,
                "approx_charging_hours": round(count * gap_seconds / 3600, 2),
            }
            for cid, count in sorted(
                connector_charging_counts.items(), key=lambda item: -item[1]
            )
        ]

        return {
            "window_hours": hours,
            "approx_poll_interval_seconds": round(gap_seconds, 1),
            "poll_count": len(poll_times),
            "by_status": by_status,
            "by_connector": by_connector,
        }
