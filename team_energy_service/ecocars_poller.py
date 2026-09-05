"""Background poller for EcoCars, mirroring evan_poller.py."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from time import monotonic

from .ecocars_provider import EcoCarsClient
from .ecocars_store import EcoCarsStore


logger = logging.getLogger(__name__)

ROLLUP_INTERVAL_SECONDS = 60 * 60  # 1 hour - see evan_poller.py for rationale


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
        self._last_rollup_at: float | None = None

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

    async def _maybe_rollup(self) -> None:
        """See EvanPoller._maybe_rollup() for the full rationale - runs
        once immediately, then at most once per ROLLUP_INTERVAL_SECONDS,
        always off the event loop via asyncio.to_thread.
        """
        now = monotonic()
        if self._last_rollup_at is not None and (
            now - self._last_rollup_at < ROLLUP_INTERVAL_SECONDS
        ):
            return
        self._last_rollup_at = now
        try:
            stats = await asyncio.to_thread(self.store.rollup_and_prune)
            logger.info("EcoCars rollup+prune: %s", stats)
        except Exception as exc:
            logger.warning("EcoCars rollup+prune failed: %s", exc)

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

            await self._maybe_rollup()

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