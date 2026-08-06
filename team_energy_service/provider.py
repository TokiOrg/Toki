from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx


API_BASE_URL = "https://api.teamenergy.am"
STATUS_LABELS = {
    "1": "available",
    "2": "busy",
    "4": "maintenance",
    "6": "charging",
}


class TeamEnergyError(RuntimeError):
    """Raised when Team Energy cannot provide a valid guest snapshot."""


class AuthenticationError(TeamEnergyError):
    """Raised when the anonymous guest token needs to be renewed."""


@dataclass(frozen=True)
class Snapshot:
    observed_at: datetime
    raw_response_hash: str
    stations: list[dict[str, Any]]
    connectors: list[dict[str, Any]]


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def derive_station_status(connector_statuses: list[str]) -> str:
    if "available" in connector_statuses:
        return "available"
    if any(status in {"busy", "charging"} for status in connector_statuses):
        return "busy"
    if connector_statuses and all(
        status == "maintenance" for status in connector_statuses
    ):
        return "maintenance"
    return "unknown"


def normalize_snapshot(
    raw_stations: list[dict[str, Any]],
    observed_at: datetime | None = None,
) -> Snapshot:
    observed_at = observed_at or datetime.now(timezone.utc)
    stations: list[dict[str, Any]] = []
    connectors: list[dict[str, Any]] = []

    for raw_station in raw_stations:
        station_id = raw_station.get("chargeStationId")
        if not isinstance(station_id, str) or not station_id:
            continue

        station_connectors: list[dict[str, Any]] = []
        for charge_point in raw_station.get("chargePointInfos") or []:
            if not isinstance(charge_point, dict):
                continue
            evse_id = charge_point.get("chargePointId")
            for connector in charge_point.get("connectors") or []:
                if not isinstance(connector, dict):
                    continue
                connector_id = connector.get("connectorId")
                if not isinstance(connector_id, str) or not connector_id:
                    continue
                status_code = str(connector.get("status", ""))
                normalized = {
                    "connector_id": connector_id,
                    "station_id": station_id,
                    "evse_id": evse_id,
                    "connector_key": connector.get("key"),
                    "evse_name": charge_point.get("stationName"),
                    "connector_number": connector.get("connectorNumber"),
                    "connector_type": connector.get("connectorType"),
                    "connector_type_group": connector.get("connectorTypeGroup"),
                    "power_kw": connector.get("power"),
                    "price": connector.get("price"),
                    "status_code": status_code,
                    "status": STATUS_LABELS.get(status_code, "unknown"),
                    "status_description": connector.get("statusDescription"),
                    "state_of_battery": connector.get("stateOfBattery"),
                    "is_preparing": connector.get("isPrepairing"),
                }
                connectors.append(normalized)
                station_connectors.append(normalized)

        stations.append(
            {
                "station_id": station_id,
                "name": raw_station.get("name"),
                "address": raw_station.get("address"),
                "phone": raw_station.get("phone"),
                "latitude": raw_station.get("latitude"),
                "longitude": raw_station.get("longitude"),
                "is_public": raw_station.get("isPublic"),
                "source_status_code": str(raw_station.get("status", "")),
                "current_status": derive_station_status(
                    [connector["status"] for connector in station_connectors]
                ),
            }
        )

    return Snapshot(
        observed_at=observed_at,
        raw_response_hash=canonical_hash(raw_stations),
        stations=stations,
        connectors=connectors,
    )


class TeamEnergyClient:
    def __init__(
        self,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=API_BASE_URL,
            timeout=timeout_seconds,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "AuthorizedTeamEnergyCollector/0.2",
                "platform": "ios",
                "appVersion": "2.1.0",
                "versionCode": "122",
            },
        )
        self._access_token: str | None = None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        access_token: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        try:
            response = await self._client.post(path, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise TeamEnergyError(f"Could not reach Team Energy: {exc}") from exc

        if response.status_code in {401, 403}:
            raise AuthenticationError(f"{path} rejected the guest token")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TeamEnergyError(
                f"{path} returned HTTP {response.status_code}"
            ) from exc

        try:
            result = response.json()
        except ValueError as exc:
            raise TeamEnergyError(f"{path} returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise TeamEnergyError(f"{path} returned an unexpected response")
        if result.get("succeeded") is not True:
            messages = result.get("messages") or result.get("errors") or []
            raise TeamEnergyError(f"{path} failed: {messages}")
        return result

    async def _guest_login(self) -> str:
        result = await self._post(
            "/UserManagement/Login",
            {"password": "", "phoneNumber": "", "guestMode": True},
        )
        data = result.get("data")
        token = data.get("accessToken") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise TeamEnergyError("Guest login did not return an access token")
        self._access_token = token
        return token

    async def fetch_snapshot(self) -> Snapshot:
        token = self._access_token or await self._guest_login()
        try:
            result = await self._post(
                "/Station/Search",
                {"noLatest": 1},
                access_token=token,
            )
        except AuthenticationError:
            token = await self._guest_login()
            result = await self._post(
                "/Station/Search",
                {"noLatest": 1},
                access_token=token,
            )

        raw_stations = result.get("data")
        if not isinstance(raw_stations, list) or not all(
            isinstance(station, dict) for station in raw_stations
        ):
            raise TeamEnergyError("Station/Search did not return a station list")
        return normalize_snapshot(raw_stations)

