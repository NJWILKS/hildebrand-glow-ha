from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_recorder_block_till_done,
)

import custom_components.hildebrand_glow.coordinator as coordinator_module
from custom_components.hildebrand_glow.api import UK_TZ, DailyReading
from custom_components.hildebrand_glow.const import CLASSIFIER_ELECTRICITY_CONSUMPTION
from custom_components.hildebrand_glow.coordinator import (
    CUMULATIVE_BACKFILL_SCHEMA_VERSION,
    GlowmarktDataUpdateCoordinator,
)


class FakeStore:
    def __init__(self, state=None) -> None:
        self.state = deepcopy(state)
        self.loads = 0
        self.saved: list[dict] = []

    async def async_load(self):
        self.loads += 1
        return deepcopy(self.state)

    async def async_save(self, data: dict) -> None:
        self.state = deepcopy(data)
        self.saved.append(deepcopy(data))


class FakeApi:
    def __init__(self) -> None:
        self._fetch_day_reading = AsyncMock(return_value=None)


def _make(hass, api: FakeApi | None = None) -> GlowmarktDataUpdateCoordinator:
    result = GlowmarktDataUpdateCoordinator(
        hass,
        api or FakeApi(),  # type: ignore[arg-type]
        {},
        virtual_entity_id="site-123",
        entry_id="entry-1",
    )
    result._store = FakeStore({})
    return result


def _ready_state(**entry_overrides) -> dict:
    entry = {
        "completed_day": "2026-09-07",
        "completed_cumulative": 10.0,
        "cumulative": 10.0,
        "live_day": None,
        "live_intervals": 0,
    }
    entry.update(entry_overrides)
    return {
        "_backfilled": True,
        "_backfilled_version": CUMULATIVE_BACKFILL_SCHEMA_VERSION,
        CLASSIFIER_ELECTRICITY_CONSUMPTION: entry,
    }


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url: str) -> None:
    assert recorder_db_url


def test_resource_and_entity_lookup_use_stable_sensor_identity(hass) -> None:
    coordinator = _make(hass)
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "resource-1"}
    }
    registry = MagicMock()
    registry.async_get_entity_id.return_value = "sensor.electricity_today"

    with patch.object(coordinator_module.er, "async_get", return_value=registry):
        assert coordinator._resource_id_for(CLASSIFIER_ELECTRICITY_CONSUMPTION) == "resource-1"
        assert coordinator._entity_id_for(CLASSIFIER_ELECTRICITY_CONSUMPTION) == "sensor.electricity_today"

    assert coordinator._resource_id_for("missing") is None
    args = registry.async_get_entity_id.call_args.args
    assert args[0] == "sensor"
    assert args[1] == coordinator_module.DOMAIN


def test_add_consumption_statistics_without_intervals_advances_baseline_only(hass) -> None:
    coordinator = _make(hass)
    reading = DailyReading(day="2026-09-01", value=1.234, intervals=[])

    assert (
        coordinator._add_consumption_statistics(
            CLASSIFIER_ELECTRICITY_CONSUMPTION,
            reading,
            10.0,
        )
        == 11.234
    )


@pytest.mark.asyncio
async def test_coordinator_clear_statistics_empty_and_recorder_callback(recorder_mock, hass) -> None:
    coordinator = _make(hass)

    await coordinator._clear_statistics([])
    await coordinator._clear_statistics(["hildebrand_glow:nonexistent_consumption_stat"])
    await async_recorder_block_till_done(hass)


@pytest.mark.asyncio
async def test_load_cumulative_caches_store_result(hass) -> None:
    coordinator = _make(hass)
    store = FakeStore({"value": 1})
    coordinator._store = store
    coordinator._cumulative = None

    first = await coordinator._load_cumulative()
    second = await coordinator._load_cumulative()

    assert first == {"value": 1}
    assert second is first
    assert store.loads == 1


@pytest.mark.asyncio
async def test_current_day_reading_rejects_day_with_no_closed_intervals(hass) -> None:
    api = FakeApi()
    coordinator = _make(hass, api)
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "resource-1"}
    }
    now = datetime(2026, 9, 8, 0, 10, tzinfo=UK_TZ)
    api._fetch_day_reading.return_value = DailyReading(
        day="2026-09-08",
        value=0.2,
        intervals=[((now - timedelta(minutes=5)).astimezone(timezone.utc), 0.2)],
    )

    assert (
        await coordinator._current_day_reading(
            CLASSIFIER_ELECTRICITY_CONSUMPTION,
            now,
        )
        is None
    )


@pytest.mark.asyncio
async def test_refresh_consumption_skips_entry_without_completed_day_or_resource_id(hass) -> None:
    coordinator = _make(hass)
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {},
    }
    coordinator._store = FakeStore(
        {
            "_backfilled": True,
            "_backfilled_version": CUMULATIVE_BACKFILL_SCHEMA_VERSION,
            CLASSIFIER_ELECTRICITY_CONSUMPTION: {
                "completed_day": None,
                "cumulative": 4.0,
            },
        }
    )

    result = await coordinator._refresh_consumption(
        datetime(2026, 9, 8, 12, 0, tzinfo=UK_TZ)
    )
    assert result[CLASSIFIER_ELECTRICITY_CONSUMPTION] is None

    coordinator._store = FakeStore(_ready_state())
    coordinator._cumulative = None
    result = await coordinator._refresh_consumption(
        datetime(2026, 9, 8, 12, 0, tzinfo=UK_TZ)
    )
    assert result[CLASSIFIER_ELECTRICITY_CONSUMPTION] is None


@pytest.mark.asyncio
async def test_closed_day_validation_error_blocks_roll_forward_and_preserves_baseline(hass) -> None:
    api = FakeApi()
    coordinator = _make(hass, api)
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "resource-1"}
    }
    coordinator._store = FakeStore(
        _ready_state(
            completed_day="2026-09-06",
            completed_cumulative=10.0,
            cumulative=10.0,
        )
    )
    full_day = datetime(2026, 9, 7, tzinfo=UK_TZ)
    api._fetch_day_reading.return_value = DailyReading(
        day="2026-09-07",
        value=4.8,
        intervals=[
            (
                (full_day + timedelta(minutes=30 * index)).astimezone(timezone.utc),
                0.1,
            )
            for index in range(48)
        ],
    )
    coordinator._add_consumption_statistics = MagicMock(  # type: ignore[method-assign]
        side_effect=ValueError("invalid")
    )

    result = await coordinator._refresh_consumption(
        datetime(2026, 9, 8, 12, 0, tzinfo=UK_TZ)
    )

    assert result[CLASSIFIER_ELECTRICITY_CONSUMPTION] == 10.0
    assert coordinator._store.state[CLASSIFIER_ELECTRICITY_CONSUMPTION][
        "completed_day"
    ] == "2026-09-06"


@pytest.mark.asyncio
async def test_missing_current_candidate_preserves_current_day_snapshot(hass) -> None:
    coordinator = _make(hass)
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "resource-1"}
    }
    coordinator._store = FakeStore(
        _ready_state(
            live_day="2026-09-08",
            live_intervals=4,
            cumulative=10.8,
        )
    )
    coordinator._current_day_reading = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await coordinator._refresh_consumption(
        datetime(2026, 9, 8, 12, 0, tzinfo=UK_TZ)
    )

    assert result[CLASSIFIER_ELECTRICITY_CONSUMPTION] == 10.8
    state = coordinator._store.state[CLASSIFIER_ELECTRICITY_CONSUMPTION]
    assert state["live_day"] == "2026-09-08"
    assert state["live_intervals"] == 4
