from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from time import monotonic

from .database import Database
from .provider import TeamEnergyClient


logger = logging.getLogger(__name__)


class Poller:
    def __init__(
        self,
        provider: TeamEnergyClient,
        database: Database,
        interval_seconds: float,
    ) -> None:
        self.provider = provider
        self.database = database
        self.interval_seconds = interval_seconds
        self._stop = asyncio.Event()

    async def poll_once(self) -> dict[str, int]:
        attempted_at = datetime.now(timezone.utc)
        await asyncio.to_thread(self.database.record_attempt, attempted_at)
        try:
            snapshot = await self.provider.fetch_snapshot()
            result = await asyncio.to_thread(self.database.record_snapshot, snapshot)
        except Exception as exc:
            failures = await asyncio.to_thread(
                self.database.record_failure,
                attempted_at,
                f"{type(exc).__name__}: {exc}",
            )
            logger.warning("Team Energy poll failed (%s): %s", failures, exc)
            raise

        logger.info(
            "Team Energy poll: %s stations, %s connectors, %s status changes",
            result["stations"],
            result["connectors"],
            result["station_changes"] + result["connector_changes"],
        )
        return result

    async def run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            started = monotonic()
            try:
                await self.poll_once()
                failures = 0
            except Exception:
                failures += 1

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

