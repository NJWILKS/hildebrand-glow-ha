from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from custom_components.hildebrand_glow import coordinator as coordinator_module
from custom_components.hildebrand_glow.api import UK_TZ, DailyReading
from custom_components.hildebrand_glow.const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_GAS_CONSUMPTION,
)
from custom_components.hildebrand_glow.coordinator import (
    CUMULATIVE_BACKFILL_SCHEMA_VERSION,
    GlowmarktDataUpdateCoordinator,
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
    )


def test_imported_total_increasing_state_matches_cumulative_sum(hass, monkeypatch) -> None:
    captured = []

    def capture_statistics(_hass, _metadata, stats) -> None:
        captured.extend(stats)

    monkeypatch.setattr(
        coordinator_module,
        "async_import_statistics",
        capture_statistics,
    )
    coordinator = _coordinator(hass, object())
    start = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)
    reading = DailyReading(
        day="2026-09-05",
        value=4.0,
        intervals=[
            (start, 1.0),
            (start + timedelta(minutes=30), 0.5),
            (start + timedelta(hours=1), 2.5),
        ],
    )

    ending = coordinator._import_hourly_statistics(
        "sensor.smart_meter_electricity_consumption",
        reading,
        100.0,
    )

    assert ending == 104.0
    assert [item["state"] for item in captured] == [101.5, 104.0]
    assert [item["sum"] for item in captured] == [101.5, 104.0]


@pytest.mark.asyncio
async def test_v23_migration_rebuilds_closed_history_without_synthetic_open_day(
    hass,
    monkeypatch,
) -> None:
    history_start = datetime(2026, 9, 5, 0, 0, tzinfo=UK_TZ)
    interval_value = 2.0 / 48
    electricity = DailyReading(
        day="2026-09-05",
        value=2.0,
        intervals=[
            (history_start + timedelta(minutes=30 * index), interval_value)
            for index in range(48)
        ],
    )
    api = type(
        "FakeApi",
        (),
        {
            "get_available_readings": AsyncMock(
                return_value={
                    CLASSIFIER_ELECTRICITY_CONSUMPTION: [electricity],
                    CLASSIFIER_GAS_CONSUMPTION: [],
                }
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
            CLASSIFIER_ELECTRICITY_CONSUMPTION: {
                "day": "2026-09-05",
                "cumulative": 2.0,
            },
        }
    )
    coordinator._entity_id_for = lambda classifier: f"sensor.{classifier.replace('.', '_')}"
    coordinator._clear_statistics = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()

    imported: list[tuple[str, list]] = []

    def capture_statistics(_hass, metadata, stats) -> None:
        imported.append((metadata["statistic_id"], list(stats)))

    monkeypatch.setattr(
        coordinator_module,
        "async_import_statistics",
        capture_statistics,
    )

    await coordinator._async_backfill_history()

    electricity_imports = [
        stats
        for statistic_id, stats in imported
        if statistic_id == "sensor.electricity_consumption"
    ]
    assert len(electricity_imports) == 1
    rebuilt = electricity_imports[0]
    assert rebuilt
    assert all(
        item["start"].astimezone(UK_TZ).date().isoformat() == "2026-09-05"
        for item in rebuilt
    )
    assert coordinator._store.state[CLASSIFIER_ELECTRICITY_CONSUMPTION] == {
        "day": "2026-09-05",
        "completed_day": "2026-09-05",
        "completed_cumulative": 2.0,
        "cumulative": 2.0,
        "live_day": None,
        "live_intervals": 0,
    }
    assert coordinator._store.state["_backfilled_version"] == (
        CUMULATIVE_BACKFILL_SCHEMA_VERSION
    )
    coordinator._clear_statistics.assert_awaited_once()


@pytest.mark.asyncio
async def test_v231_backfill_keeps_partial_old_history_and_continues(
    hass,
    freezer,
    monkeypatch,
) -> None:
    freezer.move_to("2026-09-08 12:00:00+01:00")
    first_start = datetime(2025, 7, 30, 10, 0, tzinfo=UK_TZ)
    first = DailyReading(
        day="2025-07-30",
        value=1.0,
        intervals=[
            (first_start, 0.4),
            (first_start + timedelta(minutes=30), 0.6),
        ],
    )
    second_start = datetime(2025, 7, 31, 0, 0, tzinfo=UK_TZ)
    second = DailyReading(
        day="2025-07-31",
        value=4.8,
        intervals=[
            (second_start + timedelta(minutes=30 * index), 0.1)
            for index in range(48)
        ],
    )
    api = type(
        "FakeApi",
        (),
        {
            "get_available_readings": AsyncMock(
                return_value={
                    CLASSIFIER_ELECTRICITY_CONSUMPTION: [first, second],
                    CLASSIFIER_GAS_CONSUMPTION: [],
                }
            )
        },
    )()
    coordinator = _coordinator(hass, api)
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "electricity-resource"}
    }
    coordinator._store = FakeStore()
    coordinator._entity_id_for = lambda classifier: f"sensor.{classifier.replace('.', '_')}"
    coordinator._clear_statistics = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()

    imported: list[tuple[str, list]] = []

    def capture_statistics(_hass, metadata, stats) -> None:
        imported.append((metadata["statistic_id"], list(stats)))

    monkeypatch.setattr(coordinator_module, "async_import_statistics", capture_statistics)

    await coordinator._async_backfill_history()

    dates = [
        stat["start"].astimezone(UK_TZ).date().isoformat()
        for _statistic_id, stats in imported
        for stat in stats
    ]
    assert "2025-07-30" in dates
    assert "2025-07-31" in dates
    state = coordinator._store.state[CLASSIFIER_ELECTRICITY_CONSUMPTION]
    assert state["completed_day"] == "2025-07-31"
    assert state["completed_cumulative"] == 5.8
    assert coordinator._store.state["_backfilled"] is True


@pytest.mark.asyncio
async def test_v231_backfill_defers_incomplete_yesterday(
    hass,
    freezer,
    monkeypatch,
) -> None:
    freezer.move_to("2026-09-08 12:00:00+01:00")
    complete_start = datetime(2026, 9, 6, 0, 0, tzinfo=UK_TZ)
    complete = DailyReading(
        day="2026-09-06",
        value=4.8,
        intervals=[
            (complete_start + timedelta(minutes=30 * index), 0.1)
            for index in range(48)
        ],
    )
    partial_start = datetime(2026, 9, 7, 0, 0, tzinfo=UK_TZ)
    partial = DailyReading(
        day="2026-09-07",
        value=0.2,
        intervals=[
            (partial_start, 0.1),
            (partial_start + timedelta(minutes=30), 0.1),
        ],
    )
    api = type(
        "FakeApi",
        (),
        {
            "get_available_readings": AsyncMock(
                return_value={
                    CLASSIFIER_ELECTRICITY_CONSUMPTION: [complete, partial],
                    CLASSIFIER_GAS_CONSUMPTION: [],
                }
            )
        },
    )()
    coordinator = _coordinator(hass, api)
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "electricity-resource"}
    }
    coordinator._store = FakeStore()
    coordinator._entity_id_for = lambda classifier: f"sensor.{classifier.replace('.', '_')}"
    coordinator._clear_statistics = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()

    imported: list[tuple[str, list]] = []

    def capture_statistics(_hass, metadata, stats) -> None:
        imported.append((metadata["statistic_id"], list(stats)))

    monkeypatch.setattr(coordinator_module, "async_import_statistics", capture_statistics)

    await coordinator._async_backfill_history()

    dates = {
        stat["start"].astimezone(UK_TZ).date().isoformat()
        for _statistic_id, stats in imported
        for stat in stats
    }
    assert dates == {"2026-09-06"}
    state = coordinator._store.state[CLASSIFIER_ELECTRICITY_CONSUMPTION]
    assert state["completed_day"] == "2026-09-06"
    assert state["completed_cumulative"] == 4.8


def test_history_backfill_uses_background_task(hass, monkeypatch) -> None:
    coordinator = _coordinator(hass, object())
    coordinator._entities_ready = True
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
