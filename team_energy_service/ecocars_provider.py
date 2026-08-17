"""EcoCars (api.chargersystem.com) client.

No login/token needed - the API only requires a vendor-identifying header.
This mirrors the style of provider.py/evan_provider.py but is much simpler
since there's no auth flow at all.

Data flow (per the API doc provided):
  1. GET /api/v1/stations/search/geo with a bounding box covering Armenia
     -> returns every station's id, name, geo, online flag.
  2. GET /api/v1/stations/{id} for each station id
     -> returns full connector-level detail: status, price, type, power,
        battery-charge-percentage.

Step 2 is one call PER STATION, which is heavier than Team Energy's single
fetch-all call. To keep this efficient, station details are fetched
concurrently (asyncio.gather) rather than one at a time in sequence.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx


BASE_URL = "https://api.chargersystem.com"

HEADERS = {
    "x-label": "eco_cars",
    "labelid": "b534372b-41c5-41b4-b54c-ba5e92851a79",
    "x-os": "android",
    "Accept": "application/json",
}

# A bounding box covering Armenia and the surrounding region, per the
# supplied API documentation's example.
ARMENIA_NW = "45.0,43.0"
ARMENIA_SE = "39.0,47.0"

# All connector type IDs enabled, per the documented example query.
ALL_CONNECTOR_TYPES = "1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,25,26,27,28,29,30"

CONNECTOR_TYPE_NAMES: dict[int, str] = {
    1: "Type 1 (J1772)",
    2: "Type 2 (Mennekes)",
    3: "Type 3A",
    4: "CCS Combo 2",
    8: "Tesla / Proprietary",
    25: "CHAdeMO",
    26: "Type 2 (High Power)",
    27: "Type 2 (Socket)",
    30: "GB/T",
}

STATUS_NAMES: dict[int, str] = {
    1: "available",
    2: "occupied",
    3: "charging",
    4: "reserved",
    7: "unavailable",
    11: "faulted",
    12: "offline",
}


class EcoCarsError(RuntimeError):
    """Raised when the EcoCars API returns an unexpected or error response."""


class EcoCarsClient:
    """No-auth client for the EcoCars charging network API."""

    def __init__(
        self,
        request_timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
        max_concurrent_station_fetches: int = 20,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=request_timeout_seconds,
            headers=HEADERS,
        )
        self._semaphore = asyncio.Semaphore(max_concurrent_station_fetches)

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_station_ids(self) -> list[dict[str, Any]]:
        """Return the base station list (id, name, geo, online) for every
        station in the configured bounding box.
        """
        response = await self._client.get(
            "/api/v1/stations/search/geo",
            params={
                "nw": ARMENIA_NW,
                "se": ARMENIA_SE,
                "types": ALL_CONNECTOR_TYPES,
                "isOffline": 1,
                "isWithErrors": 1,
                "isNotManageable": 1,
            },
        )
        if response.status_code != 200:
            raise EcoCarsError(
                f"EcoCars geo search failed: {response.status_code} {response.text[:300]}"
            )
        payload = response.json()
        if not isinstance(payload, list):
            raise EcoCarsError(f"Unexpected geo search response shape: {payload}")
        return payload

    async def _fetch_station_detail(self, station_id: int) -> dict[str, Any] | None:
        async with self._semaphore:
            try:
                response = await self._client.get(f"/api/v1/stations/{station_id}")
            except httpx.HTTPError:
                return None
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    async def fetch_all_stations(self) -> list[dict[str, Any]]:
        """Fetch every station's full connector-level detail.

        Step 1 gets the id list; step 2 fetches each station's detail
        concurrently (bounded by max_concurrent_station_fetches) so one poll
        cycle doesn't take N sequential round-trips.
        """
        station_ids_payload = await self.fetch_station_ids()
        station_ids = [
            entry["id"] for entry in station_ids_payload if isinstance(entry, dict) and "id" in entry
        ]
        details = await asyncio.gather(
            *(self._fetch_station_detail(sid) for sid in station_ids)
        )
        return [detail for detail in details if detail is not None]
