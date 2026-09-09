from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.hildebrand_glow.api import UK_TZ, DailyReading
from custom_components.hildebrand_glow.const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_GAS_CONSUMPTION,
)
from custom_components.hildebrand_glow.ledger_coordinator import (
    LedgerOnlyGlowmarktDataUpdateCoordinator,
)


def _coordinator(hass) -> LedgerOnlyGlowmarktDataUpdateCoordinator:
    coordinator = LedgerOnlyGlowmarktDataUpdateCoordinator(
        hass=hass,
        api_client=MagicMock(),
        tariff_config={},
        entry_id="entry-1",
    )
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "electricity"},
        CLASSIFIER_GAS_CONSUMPTION: {"resource_id": "gas"},
    }
    return coordinator


@pytest.mark.asyncio
async def test_refresh_consumption_updates_presentation_only(hass) -> None:
    coordinator = _coordinator(hass)
    now = datetime(2026, 9, 9, 17, 0, tzinfo=UK_TZ)
    electricity = DailyReading(
        day="2026-09-09",
        value=1.23456,
        intervals=[],
    )
    coordinator._current_day_reading = AsyncMock(
        side_effect=[electricity, None]
    )
    coordinator._add_consumption_statistics = MagicMock()
    coordinator._load_cumulative = AsyncMock()

    result = await coordinator._refresh_consumption(now)

    assert result == {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: 1.235,
        CLASSIFIER_GAS_CONSUMPTION: None,
    }
    assert coordinator._last_readings[CLASSIFIER_ELECTRICITY_CONSUMPTION] is electricity
    coordinator._add_consumption_statistics.assert_not_called()
    coordinator._load_cumulative.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_consumption_preserves_same_day_cache_but_not_yesterday(hass) -> None:
    coordinator = _coordinator(hass)
    now = datetime(2026, 9, 9, 17, 0, tzinfo=UK_TZ)
    coordinator._last_readings = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: DailyReading(
            day="2026-09-09",
            value=2.5,
            intervals=[],
        ),
        CLASSIFIER_GAS_CONSUMPTION: DailyReading(
            day="2026-09-08",
            value=3.5,
            intervals=[],
        ),
    }
    coordinator._current_day_reading = AsyncMock(return_value=None)

    result = await coordinator._refresh_consumption(now)

    assert result[CLASSIFIER_ELECTRICITY_CONSUMPTION] == 2.5
    assert result[CLASSIFIER_GAS_CONSUMPTION] is None
    assert CLASSIFIER_GAS_CONSUMPTION not in coordinator._last_readings


def test_legacy_backfill_entry_points_are_noops(hass) -> None:
    coordinator = _coordinator(hass)
    coordinator._entities_ready = False
    hass.async_create_background_task = MagicMock()

    coordinator.schedule_history_backfill()
    coordinator.start_history_backfill()

    assert coordinator._entities_ready is False
    assert coordinator._backfill_started is False
    assert coordinator._history_start_scheduled is False
    hass.async_create_background_task.assert_not_called()
