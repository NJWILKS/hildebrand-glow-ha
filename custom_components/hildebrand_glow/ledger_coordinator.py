"""Ledger-only coordinator behaviour for the 2.5 history rebuild.

The existing coordinator still contains the 2.4 statistics/backfill machinery for
compatibility and regression coverage.  This subclass is the runtime coordinator
for the 2.5 feature branch: it keeps presentation sensors current while making the
new PT30M ledger the only historical population job.
"""
from __future__ import annotations

from datetime import datetime

from .const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_GAS_CONSUMPTION,
)
from .coordinator import GlowmarktDataUpdateCoordinator

CURRENT_CONSUMPTION_CLASSIFIERS = (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_GAS_CONSUMPTION,
)


class LedgerOnlyGlowmarktDataUpdateCoordinator(GlowmarktDataUpdateCoordinator):
    """Keep current presentation values without touching Recorder history."""

    async def _refresh_consumption(
        self,
        now_uk: datetime,
    ) -> dict[str, float | None]:
        """Refresh today's closed PT30M consumption without writing statistics."""
        result: dict[str, float | None] = {
            classifier: None for classifier in CURRENT_CONSUMPTION_CLASSIFIERS
        }
        today = now_uk.date().isoformat()

        for classifier in CURRENT_CONSUMPTION_CLASSIFIERS:
            if classifier not in self._resources:
                continue

            candidate = await self._current_day_reading(classifier, now_uk)
            if candidate is not None:
                self._last_readings[classifier] = candidate
                result[classifier] = round(float(candidate.value), 3)
                continue

            # A transient shorter/empty API response must not make a current-day
            # presentation sensor jump backwards or become unavailable.  Keep the
            # last same-day value, but never carry yesterday into a new UK day.
            cached = self._last_readings.get(classifier)
            if cached is not None and cached.day == today:
                result[classifier] = round(float(cached.value), 3)
            elif cached is not None:
                self._last_readings.pop(classifier, None)

        return result

    def start_history_backfill(self) -> None:
        """Disable the superseded 2.4 Recorder history backfill."""
        return

    def schedule_history_backfill(self, delay: int = 0) -> None:
        """Disable the superseded delayed 2.4 Recorder history backfill."""
        return
