"""Standalone Evan (evcharge-api-prod.e-evan.com) client.

This mirrors the style of provider.py (the Team Energy client) but is kept
self-contained and does not touch the existing Team Energy database/poller
pipeline. Evan requires a real logged-in account (phone + password), unlike
Team Energy's anonymous guest session, so the token lifecycle is different:

- accessToken:  short-lived (observed: 15 minutes / 900000 ms)
- refreshToken: long-lived  (observed: 60 days / 5,184,000,000 ms)

Usage:
    client = EvanClient(phone="+374...", password="...")
    stations = await client.fetch_stations(lat=40.1776, lng=44.5126, radius_deg=0.05)
    await client.close()

The first call automatically logs in with phone+password (no OTP needed after
the account's initial device verification is already done). After that, the
client refreshes the access token automatically as it nears expiry.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


BASE_URL = "https://evcharge-api-prod.e-evan.com"

# Refresh a bit before the token actually expires, to avoid racing a request
# against expiry.
REFRESH_SAFETY_MARGIN_SECONDS = 60


class EvanError(RuntimeError):
    """Raised when the Evan API returns an unexpected or error response."""


@dataclass
class EvanTokens:
    access_token: str
    refresh_token: str
    access_expires_at_ms: int  # epoch milliseconds

    @property
    def access_expires_soon(self) -> bool:
        now_ms = time.time() * 1000
        return now_ms >= (self.access_expires_at_ms - REFRESH_SAFETY_MARGIN_SECONDS * 1000)


class EvanClient:
    """Authenticated client for Evan's charging-network API.

    Holds tokens in memory only. For a long-running service, persist
    `tokens.refresh_token` somewhere (e.g. a settings table) so a restart
    doesn't require a fresh password login.
    """

    def __init__(
        self,
        phone: str | None = None,
        password: str | None = None,
        request_timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
        initial_refresh_token: str | None = None,
    ) -> None:
        self._phone = phone
        self._password = password
        self._client = client or httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=request_timeout_seconds,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
                # A generic identifying UA. Evan's app sends a lot of
                # device-fingerprint headers (x-app-device-*); none of them
                # were required for the endpoints exercised so far.
                "User-Agent": "TokiEvanClient/1.0",
            },
        )
        self._tokens: EvanTokens | None = None
        self._pending_refresh_token = initial_refresh_token

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def _login_with_password(self) -> EvanTokens:
        if not self._phone or not self._password:
            raise EvanError(
                "No refresh token available and no phone/password provided "
                "to log in with."
            )
        # Confirmed via captured traffic: the real path is /signin, not /login.
        response = await self._client.post(
            "/api/users/auth/signin",
            json={"phone": self._phone, "password": self._password},
        )
        return self._parse_token_response(response)

    async def _refresh(self, refresh_token: str) -> EvanTokens:
        # NOTE: same caveat as above - confirm the exact refresh path by
        # capturing Evan's own app performing a refresh (leave the app open
        # past the 15-minute access-token lifetime and watch mitmproxy).
        response = await self._client.post(
            "/api/users/auth/refresh",
            json={"refreshToken": refresh_token},
        )
        return self._parse_token_response(response)

    @staticmethod
    def _parse_token_response(response: httpx.Response) -> EvanTokens:
        if response.status_code != 200:
            raise EvanError(
                f"Evan auth call failed: {response.status_code} {response.text[:300]}"
            )
        payload = response.json()
        try:
            token = payload["data"]["token"]
            return EvanTokens(
                access_token=token["accessToken"],
                refresh_token=token["refreshToken"],
                access_expires_at_ms=int(token["accessExpiresAt"]),
            )
        except (KeyError, TypeError) as exc:
            raise EvanError(f"Unexpected Evan auth response shape: {payload}") from exc

    async def _ensure_token(self) -> str:
        if self._tokens is not None and not self._tokens.access_expires_soon:
            return self._tokens.access_token

        if self._tokens is not None:
            # Have a (soon-to-expire) refresh token - use it.
            self._tokens = await self._refresh(self._tokens.refresh_token)
        elif self._pending_refresh_token:
            # Resuming from a previously stored refresh token.
            self._tokens = await self._refresh(self._pending_refresh_token)
            self._pending_refresh_token = None
        else:
            self._tokens = await self._login_with_password()

        return self._tokens.access_token

    @property
    def refresh_token(self) -> str | None:
        """Expose the current refresh token so a caller can persist it."""
        return self._tokens.refresh_token if self._tokens else None

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    async def _authed_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        token = await self._ensure_token()
        response = await self._client.get(
            path,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 401:
            # Access token rejected outright (e.g. revoked) - force one
            # refresh-or-login cycle and retry exactly once.
            self._tokens = None
            token = await self._ensure_token()
            response = await self._client.get(
                path,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code != 200:
            raise EvanError(
                f"Evan API call failed: {response.status_code} {response.text[:300]}"
            )
        return response.json()

    async def fetch_stations_raw(
        self,
        lat: float,
        lng: float,
        lat_span_deg: float = 0.05,
        lng_span_deg: float = 0.05,
        zoom: float = 12.0,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Fetch raw station records within a map bounding box.

        Evan's /api/stations/stations endpoint is viewport-scoped (it takes a
        lat/lng bounding box, not "give me everything"). To cover a whole
        city or country you need to tile several calls across a grid of
        boxes and de-duplicate by station id - see fetch_all_stations().
        """
        boundaries = {
            "zoom": zoom,
            "center": {"lat": lat, "lng": lng},
            "latMin": lat - lat_span_deg / 2,
            "latMax": lat + lat_span_deg / 2,
            "lngMin": lng - lng_span_deg / 2,
            "lngMax": lng + lng_span_deg / 2,
        }
        filter_ = {
            "isAvailable": False,
            "isNonRestricted": False,
            "onlyCorporate": False,
            "onlyFavorites": False,
            "power": [0, 100000],
        }
        import json as _json

        params = {
            "_limit": limit,
            "_offset": 0,
            "filter": "object:" + _json.dumps(filter_, separators=(",", ":")),
            "boundaries": "object:" + _json.dumps(boundaries, separators=(",", ":")),
        }
        payload = await self._authed_get("/api/stations/stations", params)
        # Confirmed real shape: {"data": {"stations": [...]}, "metadata": {...}}
        data = payload.get("data") or {}
        stations = data.get("stations")
        if not isinstance(stations, list):
            raise EvanError(f"Unexpected /stations/stations shape: {payload}")
        return stations

    async def fetch_all_stations(
        self,
        grid_centers: list[tuple[float, float]],
        lat_span_deg: float = 0.05,
        lng_span_deg: float = 0.05,
    ) -> list[dict[str, Any]]:
        """Fetch and de-duplicate stations across several map tiles.

        `grid_centers` is a list of (lat, lng) points covering the area you
        care about (e.g. Yerevan plus a few regional towns). Since the
        endpoint is viewport-scoped, one call only sees what's visible in
        that box - tile enough boxes to cover the whole region.
        """
        seen: dict[str, dict[str, Any]] = {}
        for lat, lng in grid_centers:
            stations = await self.fetch_stations_raw(
                lat, lng, lat_span_deg=lat_span_deg, lng_span_deg=lng_span_deg
            )
            for station in stations:
                station_id = station.get("id") or station.get("_id")
                if station_id is not None:
                    seen[str(station_id)] = station
        return list(seen.values())

    async def fetch_my_transactions(self) -> list[dict[str, Any]]:
        """Fetch the logged-in account's own charging transactions.

        This is account-scoped data (yours only) - fine to poll for
        self-calibration (real kWh/AMD per session), unlike the network-wide
        station feed above.
        """
        payload = await self._authed_get(
            "/api/chargings/charge-transactions",
            {"isBillShown": "is_equal:false"},
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise EvanError(f"Unexpected /charge-transactions shape: {payload}")
        return data


   
     
