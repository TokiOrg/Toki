"""Background poller for Evan, mirroring team_energy_service/poller.py.

Kept fully separate from the Team Energy Poller/Database - different client,
different store, own failure/backoff handling. Wiring this into the app's
lifespan (see api.py) runs it alongside the existing Team Energy poller
without touching it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from time import monotonic

from .evan_provider import EvanClient
from .evan_store import EvanStore


logger = logging.getLogger(__name__)


class EvanPoller:
    def __init__(
        self,
        client: EvanClient,
        store: EvanStore,
        interval_seconds: float,
        grid_centers: list[tuple[float, float]],
    ) -> None:
        self.client = client
        self.store = store
        self.interval_seconds = interval_seconds
        self.grid_centers = grid_centers
        self._stop = asyncio.Event()

    async def poll_once(self) -> int:
        polled_at = datetime.now(timezone.utc)
        stations = await self.client.fetch_all_stations(self.grid_centers)
        rows_written = await asyncio.to_thread(
            self.store.record_snapshot, stations, polled_at
        )
        logger.info(
            "Evan poll: %s stations, %s connector rows written",
            len(stations),
            rows_written,
        )
        return rows_written

    async def run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            started = monotonic()
            try:
                await self.poll_once()
                failures = 0
            except Exception as exc:
                failures += 1
                logger.warning("Evan poll failed (%s): %s", failures, exc)

            elapsed = monotonic() - started
            if failures:
                target_delay = min(
                    self.interval_seconds * (2 ** min(failures, 4)),
                    300.0,
                )
            else:
                target_delay = self.interval_seconds
            delay = max(0.0, target_delay - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
