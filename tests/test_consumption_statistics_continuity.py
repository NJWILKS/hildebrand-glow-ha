from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from custom_components.hildebrand_glow import coordinator as coordinator_module
from custom_components.hildebrand_glow.api import UK_TZ, DailyReading
from custom_components.hildebrand_glow.const import CLASSIFIER_ELECTRICITY_CONSUMPTION
from custom_components.hildebrand_glow.coordinator import (
    CUMULATIVE_BACKFILL_SCHEMA_VERSION,
    GlowmarktDataUpdateCoordinator,
)
from custom_components.hildebrand_glow.consumption_statistics import (
    energy_consumption_statistic_id,
)


class FakeStore:
    def __init__(self, state: dict | None = None) -> None:
        self.state = deepcopy(state or {})

    async def async_load(self) -> dict:
        return deepcopy(self.state)

    async def async_save(self, data: dict) -> None:
        self.state = deepcopy(data)


def _coordinator(hass, api) -> GlowmarktDataUpdateCoordinator:
    return GlowmarktDataUpdateCoordinator(
        hass,
        api,
        {
            "electricity_rate": 0.25,
            "electricity_standing_charge": 0.50,
            "gas_rate": 0.07,
            "gas_standing_charge": 0.30,
        },
        virtual_entity_id="site-123",
    )


def _complete(day: str, value: float) -> DailyReading:
    start = datetime.fromisoformat(day).replace(tzinfo=UK_TZ)
    values = [value / 48.0] * 48
    return DailyReading(
        day=day,
        value=value,
        intervals=[
            (start + timedelta(minutes=30 * index), interval)
            for index, interval in enumerate(values)
        ],
    )


@pytest.mark.asyncio
async def test_schema_upgrade_clears_legacy_and_external_stats_then_migrates_energy(
    hass,
    freezer,
    monkeypatch,
) -> None:
    freezer.move_to("2026-09-08 12:00:00+01:00")
    reading = _complete("2026-09-05", 2.0)
    api = type(
        "FakeApi",
        (),
        {
            "get_available_readings": AsyncMock(
                return_value={CLASSIFIER_ELECTRICITY_CONSUMPTION: [reading]}
            )
        },
    )()
    coordinator = _coordinator(hass, api)
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "electricity-resource"}
    }
    coordinator._store = FakeStore(
        {
            "_backfilled": True,
            "_backfilled_version": CUMULATIVE_BACKFILL_SCHEMA_VERSION - 1,
        }
    )
    coordinator._entity_id_for = lambda _classifier: "sensor.legacy_electricity"
    coordinator._clear_statistics = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()

    imported: list[tuple[str, str, float]] = []

    def capture(_hass, site_id, commodity, readings, *, baseline=0.0):
        ending = baseline + sum(item.value for item in readings)
        imported.append((site_id, commodity, ending))
        return [], ending

    monkeypatch.setattr(coordinator_module, "add_consumption_statistics", capture)
    migrate = AsyncMock(return_value=True)
    monkeypatch.setattr(
        coordinator_module,
        "async_migrate_energy_consumption_statistics",
        migrate,
    )

    await coordinator._async_backfill_history()

    external_id = energy_consumption_statistic_id("site-123", "electricity")
    coordinator._clear_statistics.assert_awaited_once_with(
        ["sensor.legacy_electricity", external_id]
    )
    assert imported == [("site-123", "electricity", 2.0)]
    migrate.assert_awaited_once_with(
        hass,
        {"sensor.legacy_electricity": external_id},
    )
    assert coordinator._store.state[CLASSIFIER_ELECTRICITY_CONSUMPTION][
        "completed_cumulative"
    ] == 2.0
    assert coordinator._store.state["_backfilled_version"] == (
        CUMULATIVE_BACKFILL_SCHEMA_VERSION
    )


@pytest.mark.asyncio
async def test_backfill_keeps_partial_old_history_but_defers_incomplete_yesterday(
    hass,
    freezer,
    monkeypatch,
) -> None:
    freezer.move_to("2026-09-08 12:00:00+01:00")
    old_start = datetime(2025, 7, 30, 10, 0, tzinfo=UK_TZ)
    old_partial = DailyReading(
        day="2025-07-30",
        value=1.0,
        intervals=[(old_start, 0.4), (old_start + timedelta(minutes=30), 0.6)],
    )
    complete = _complete("2026-09-06", 4.8)
    trailing_start = datetime(2026, 9, 7, 0, 0, tzinfo=UK_TZ)
    trailing = DailyReading(
        day="2026-09-07",
        value=0.2,
        intervals=[
            (trailing_start, 0.1),
            (trailing_start + timedelta(minutes=30), 0.1),
        ],
    )
    api = type(
        "FakeApi",
        (),
        {
            "get_available_readings": AsyncMock(
                return_value={
                    CLASSIFIER_ELECTRICITY_CONSUMPTION: [
                        old_partial,
                        complete,
                        trailing,
                    ]
                }
            )
        },
    )()
    coordinator = _coordinator(hass, api)
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "electricity-resource"}
    }
    coordinator._store = FakeStore()
    coordinator._entity_id_for = lambda _classifier: "sensor.legacy_electricity"
    coordinator._clear_statistics = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()

    imported_days: list[str] = []

    def capture(_hass, _site_id, _commodity, readings, *, baseline=0.0):
        imported_days.extend(item.day for item in readings)
        ending = baseline + sum(item.value for item in readings)
        return [], ending

    monkeypatch.setattr(coordinator_module, "add_consumption_statistics", capture)
    monkeypatch.setattr(
        coordinator_module,
        "async_migrate_energy_consumption_statistics",
        AsyncMock(return_value=True),
    )

    await coordinator._async_backfill_history()

    assert imported_days == ["2025-07-30", "2026-09-06"]
    state = coordinator._store.state[CLASSIFIER_ELECTRICITY_CONSUMPTION]
    assert state["completed_day"] == "2026-09-06"
    assert state["completed_cumulative"] == 5.8


def test_history_backfill_uses_background_task(hass, monkeypatch) -> None:
    coordinator = _coordinator(hass, object())
    coordinator._entities_ready = True
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "electricity-resource"}
    }
    coordinator._entity_id_for = lambda _classifier: "sensor.electricity_consumption"
    coordinator._async_backfill_history = AsyncMock()

    created: list[str] = []
    original = hass.async_create_background_task

    def capture_background_task(target, name, *, eager_start=False):
        created.append(name)
        return original(target, name, eager_start=eager_start)

    monkeypatch.setattr(hass, "async_create_background_task", capture_background_task)

    coordinator.start_history_backfill()

    assert created == ["hildebrand_glow history backfill"]
