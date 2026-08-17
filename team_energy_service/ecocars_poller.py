"""Background poller for EcoCars, mirroring evan_poller.py."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from time import monotonic

from .ecocars_provider import EcoCarsClient
from .ecocars_store import EcoCarsStore


logger = logging.getLogger(__name__)


class EcoCarsPoller:
    def __init__(
        self,
        client: EcoCarsClient,
        store: EcoCarsStore,
        interval_seconds: float,
    ) -> None:
        self.client = client
        self.store = store
        self.interval_seconds = interval_seconds
        self._stop = asyncio.Event()

    async def poll_once(self) -> int:
        polled_at = datetime.now(timezone.utc)
        stations = await self.client.fetch_all_stations()
        rows_written = await asyncio.to_thread(
            self.store.record_snapshot, stations, polled_at
        )
        logger.info(
            "EcoCars poll: %s stations, %s connector rows written",
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
                logger.warning("EcoCars poll failed (%s): %s", failures, exc)

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
