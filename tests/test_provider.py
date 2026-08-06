from __future__ import annotations

import asyncio
import json

import httpx

from team_energy_service.provider import TeamEnergyClient

from .test_database import raw_snapshot


def test_provider_reuses_one_guest_token_across_polls():
    calls = {"login": 0, "search": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/UserManagement/Login":
            calls["login"] += 1
            assert json.loads(request.content) == {
                "password": "",
                "phoneNumber": "",
                "guestMode": True,
            }
            return httpx.Response(
                200,
                json={
                    "succeeded": True,
                    "errors": [],
                    "messages": [],
                    "data": {"accessToken": "guest-token"},
                },
            )
        if request.url.path == "/Station/Search":
            calls["search"] += 1
            assert request.headers["Authorization"] == "Bearer guest-token"
            return httpx.Response(
                200,
                json={
                    "succeeded": True,
                    "errors": [],
                    "messages": [],
                    "data": raw_snapshot(1),
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def run_test() -> None:
        http_client = httpx.AsyncClient(
            base_url="https://api.teamenergy.am",
            transport=httpx.MockTransport(handler),
        )
        provider = TeamEnergyClient(client=http_client)
        try:
            await provider.fetch_snapshot()
            await provider.fetch_snapshot()
        finally:
            await http_client.aclose()

    asyncio.run(run_test())
    assert calls == {"login": 1, "search": 2}
