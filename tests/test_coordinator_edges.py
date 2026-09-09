from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

import custom_components.hildebrand_glow.coordinator as coordinator_module
from custom_components.hildebrand_glow.api import (
    UK_TZ,
    DailyReading,
    GlowmarktApiError,
    GlowmarktAuthError,
)
from custom_components.hildebrand_glow.const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_ELECTRICITY_COST,
    CLASSIFIER_GAS_CONSUMPTION,
    CLASSIFIER_GAS_COST,
)
from custom_components.hildebrand_glow.coordinator import (
    CUMULATIVE_BACKFILL_SCHEMA_VERSION,
    GlowmarktDataUpdateCoordinator,
)
from custom_components.hildebrand_glow.costing import CostBreakdown


class FakeStore:
    def __init__(self, state: dict | None = None) -> None:
        self.state = deepcopy(state or {})
        self.saved: list[dict] = []

    async def async_load(self) -> dict:
        return deepcopy(self.state)

    async def async_save(self, data: dict) -> None:
        self.state = deepcopy(data)
        self.saved.append(deepcopy(data))


class FakeApi:
    def __init__(self) -> None:
        self.discover_resources = AsyncMock(return_value={})
        self.get_available_readings = AsyncMock(return_value={})
        self._fetch_day_reading = AsyncMock(return_value=None)


def _make(hass, api: FakeApi | None = None) -> GlowmarktDataUpdateCoordinator:
    client = api or FakeApi()
    result = GlowmarktDataUpdateCoordinator(
        hass,
        client,  # type: ignore[arg-type]
        {
            "electricity_rate": 0.25,
            "electricity_standing_charge": 0.50,
            "gas_rate": 0.07,
            "gas_standing_charge": 0.30,
        },
        virtual_entity_id="site-123",
        entry_id="entry-1",
        consumption_interval_minutes=15,
        cost_interval_minutes=60,
    )
    result._store = FakeStore()
    return result


def _ready_state(
    *,
    completed_day: str = "2026-09-07",
    live_day: str | None = None,
    live_intervals: int = 0,
    cumulative: float = 10.0,
) -> dict:
    return {
        "_backfilled": True,
        "_backfilled_version": CUMULATIVE_BACKFILL_SCHEMA_VERSION,
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {
            "completed_day": completed_day,
            "completed_cumulative": 10.0,
            "cumulative": cumulative,
            "live_day": live_day,
            "live_intervals": live_intervals,
        },
    }


def _reading(day: str, count: int, value: float = 0.1) -> DailyReading:
    start = datetime.fromisoformat(day).replace(tzinfo=UK_TZ)
    return DailyReading(
        day=day,
        value=round(count * value, 3),
        intervals=[
            (start.astimezone(timezone.utc) + timedelta(minutes=30 * index), value)
            for index in range(count)
        ],
    )


def test_expected_interval_count_handles_uk_dst_days() -> None:
    assert GlowmarktDataUpdateCoordinator._expected_intervals(date(2026, 3, 28)) == 48
    assert GlowmarktDataUpdateCoordinator._expected_intervals(date(2026, 3, 29)) == 46
    assert GlowmarktDataUpdateCoordinator._expected_intervals(date(2026, 10, 25)) == 50


@pytest.mark.asyncio
async def test_current_day_reading_handles_missing_resource_empty_and_open_interval(hass) -> None:
    api = FakeApi()
    coordinator = _make(hass, api)
    now = datetime(2026, 9, 8, 12, 15, tzinfo=UK_TZ)

    assert (
        await coordinator._current_day_reading(
            CLASSIFIER_ELECTRICITY_CONSUMPTION,
            now,
        )
        is None
    )
    api._fetch_day_reading.assert_not_awaited()

    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "electricity"}
    }
    api._fetch_day_reading.return_value = None
    assert (
        await coordinator._current_day_reading(
            CLASSIFIER_ELECTRICITY_CONSUMPTION,
            now,
        )
        is None
    )

    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    api._fetch_day_reading.return_value = DailyReading(
        day="2026-09-08",
        value=0.6,
        intervals=[
            (start.astimezone(timezone.utc), 0.1),
            ((now - timedelta(minutes=20)).astimezone(timezone.utc), 0.2),
            ((now - timedelta(minutes=5)).astimezone(timezone.utc), 0.3),
        ],
    )
    result = await coordinator._current_day_reading(
        CLASSIFIER_ELECTRICITY_CONSUMPTION,
        now,
    )
    assert result is not None
    assert result.value == 0.1
    assert len(result.intervals) == 1


@pytest.mark.asyncio
async def test_refresh_consumption_waits_for_backfill_schema(hass) -> None:
    coordinator = _make(hass)
    coordinator._store = FakeStore({"_backfilled": False})

    result = await coordinator._refresh_consumption(
        datetime(2026, 9, 8, 12, 0, tzinfo=UK_TZ)
    )

    assert result == {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: None,
        CLASSIFIER_GAS_CONSUMPTION: None,
    }


@pytest.mark.asyncio
async def test_refresh_consumption_preserves_live_value_when_closed_gap_is_incomplete(hass) -> None:
    api = FakeApi()
    coordinator = _make(hass, api)
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "electricity"}
    }
    coordinator._store = FakeStore(
        _ready_state(
            completed_day="2026-09-06",
            live_day="2026-09-07",
            live_intervals=12,
            cumulative=14.2,
        )
    )
    api._fetch_day_reading.return_value = _reading("2026-09-07", 1)

    result = await coordinator._refresh_consumption(
        datetime(2026, 9, 8, 12, 0, tzinfo=UK_TZ)
    )

    assert result[CLASSIFIER_ELECTRICITY_CONSUMPTION] == 14.2
    state = coordinator._store.state[CLASSIFIER_ELECTRICITY_CONSUMPTION]
    assert state["completed_day"] == "2026-09-06"
    assert state["live_intervals"] == 12


@pytest.mark.asyncio
async def test_refresh_consumption_preserves_more_complete_current_day_snapshot(hass) -> None:
    api = FakeApi()
    coordinator = _make(hass, api)
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "electricity"}
    }
    coordinator._store = FakeStore(
        _ready_state(
            completed_day="2026-09-07",
            live_day="2026-09-08",
            live_intervals=10,
            cumulative=12.5,
        )
    )
    candidate = _reading("2026-09-08", 4)
    coordinator._current_day_reading = AsyncMock(return_value=candidate)  # type: ignore[method-assign]

    result = await coordinator._refresh_consumption(
        datetime(2026, 9, 8, 12, 0, tzinfo=UK_TZ)
    )

    assert result[CLASSIFIER_ELECTRICITY_CONSUMPTION] == 12.5
    state = coordinator._store.state[CLASSIFIER_ELECTRICITY_CONSUMPTION]
    assert state["live_intervals"] == 10


@pytest.mark.asyncio
async def test_refresh_consumption_rejects_invalid_current_day_and_preserves_last_good(hass) -> None:
    coordinator = _make(hass)
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "electricity"}
    }
    coordinator._store = FakeStore(
        _ready_state(
            completed_day="2026-09-07",
            live_day="2026-09-08",
            live_intervals=2,
            cumulative=10.7,
        )
    )
    coordinator._current_day_reading = AsyncMock(  # type: ignore[method-assign]
        return_value=_reading("2026-09-08", 3)
    )
    coordinator._add_consumption_statistics = MagicMock(  # type: ignore[method-assign]
        side_effect=ValueError("bad interval")
    )

    result = await coordinator._refresh_consumption(
        datetime(2026, 9, 8, 12, 0, tzinfo=UK_TZ)
    )

    assert result[CLASSIFIER_ELECTRICITY_CONSUMPTION] == 10.7
    assert coordinator._store.state[CLASSIFIER_ELECTRICITY_CONSUMPTION][
        "live_intervals"
    ] == 2


@pytest.mark.asyncio
async def test_refresh_consumption_clears_stale_cached_day_when_today_has_no_data(hass) -> None:
    coordinator = _make(hass)
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "electricity"}
    }
    coordinator._store = FakeStore(_ready_state(completed_day="2026-09-07"))
    coordinator._last_readings[CLASSIFIER_ELECTRICITY_CONSUMPTION] = _reading(
        "2026-09-07", 1
    )
    coordinator._current_day_reading = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await coordinator._refresh_consumption(
        datetime(2026, 9, 8, 12, 0, tzinfo=UK_TZ)
    )

    assert CLASSIFIER_ELECTRICITY_CONSUMPTION not in coordinator._last_readings


@pytest.mark.asyncio
async def test_history_backfill_start_guards_and_schedules_once(hass) -> None:
    coordinator = _make(hass)
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "electricity"}
    }
    coordinator._async_backfill_history = AsyncMock()  # type: ignore[method-assign]

    coordinator.start_history_backfill()
    assert coordinator._history_task is None

    coordinator._entities_ready = True
    coordinator._entity_id_for = MagicMock(return_value=None)  # type: ignore[method-assign]
    coordinator.start_history_backfill()
    assert coordinator._history_task is None

    coordinator._entity_id_for = MagicMock(return_value="sensor.electricity")  # type: ignore[method-assign]
    coordinator.start_history_backfill()
    assert coordinator._history_task is not None
    await coordinator._history_task
    assert coordinator._backfill_started is True

    existing = coordinator._history_task
    coordinator.start_history_backfill()
    assert coordinator._history_task is existing


@pytest.mark.asyncio
async def test_schedule_history_backfill_resets_delay_flag_and_avoids_duplicates(hass) -> None:
    coordinator = _make(hass)
    coordinator.start_history_backfill = MagicMock()  # type: ignore[method-assign]

    coordinator.schedule_history_backfill(delay=0)
    first = coordinator._delayed_history_task
    coordinator.schedule_history_backfill(delay=0)

    assert coordinator._delayed_history_task is first
    assert first is not None
    await first
    coordinator.start_history_backfill.assert_called_once()
    assert coordinator._history_start_scheduled is False


@pytest.mark.asyncio
async def test_backfill_short_circuits_when_current_schema_already_complete(hass) -> None:
    api = FakeApi()
    coordinator = _make(hass, api)
    coordinator._store = FakeStore(
        {
            "_backfilled": True,
            "_backfilled_version": CUMULATIVE_BACKFILL_SCHEMA_VERSION,
        }
    )
    coordinator._backfill_started = True

    await coordinator._async_backfill_history()

    api.get_available_readings.assert_not_awaited()
    assert coordinator._backfill_started is True


@pytest.mark.asyncio
async def test_backfill_retries_when_entity_registration_is_missing(hass) -> None:
    api = FakeApi()
    api.get_available_readings.return_value = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: []
    }
    coordinator = _make(hass, api)
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "electricity"}
    }
    coordinator._entity_id_for = MagicMock(return_value=None)  # type: ignore[method-assign]
    coordinator._backfill_started = True
    coordinator._clear_statistics = AsyncMock()  # type: ignore[method-assign]

    await coordinator._async_backfill_history()

    coordinator._clear_statistics.assert_not_awaited()
    assert coordinator._backfill_started is False


@pytest.mark.asyncio
async def test_backfill_failure_releases_retry_guard(hass) -> None:
    api = FakeApi()
    api.get_available_readings.side_effect = RuntimeError("boom")
    coordinator = _make(hass, api)
    coordinator._backfill_started = True

    await coordinator._async_backfill_history()

    assert coordinator._backfill_started is False


def test_cost_refresh_due_uses_configured_interval(hass) -> None:
    coordinator = _make(hass)
    now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)

    assert coordinator._cost_refresh_due(now) is True
    coordinator._last_cost_refresh_at = now - timedelta(minutes=59)
    assert coordinator._cost_refresh_due(now) is False
    coordinator._last_cost_refresh_at = now - timedelta(minutes=60)
    assert coordinator._cost_refresh_due(now) is True


@pytest.mark.asyncio
async def test_cost_refresh_handles_missing_none_error_and_success(hass) -> None:
    coordinator = _make(hass)
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_COST: {"resource_id": "electricity-cost"},
        CLASSIFIER_GAS_COST: {"resource_id": "gas-cost"},
    }
    now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    breakdown = CostBreakdown(
        day="2026-09-08",
        total_pence=100.0,
        usage_pence=50.0,
        standing_charge_pence=50.0,
        standing_charge_status="applied",
        complete_day=False,
        usage_intervals=((now, 50.0),),
    )

    async def latest(_api, resource_id, *, now_uk):
        assert now_uk.tzinfo is not None
        if resource_id == "electricity-cost":
            return breakdown
        raise GlowmarktApiError("temporary")

    coordinator._entities_ready = True
    with (
        patch.object(coordinator_module, "get_latest_cost_breakdown", side_effect=latest),
        patch.object(
            coordinator_module,
            "async_sync_cost_ingestion",
            new=AsyncMock(side_effect=RuntimeError("background failed")),
        ) as sync,
    ):
        await coordinator._refresh_cost_breakdowns(now)

    assert coordinator._cost_breakdowns["electricity"] == breakdown
    assert coordinator._last_readings[CLASSIFIER_ELECTRICITY_COST].value == 100.0
    assert coordinator._last_cost_refresh_at == now
    sync.assert_awaited_once()



def test_compose_costs_marks_unknown_standing_and_unavailable_commodity(hass) -> None:
    coordinator = _make(hass)
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_COST: {"resource_id": "electricity-cost"},
        CLASSIFIER_GAS_CONSUMPTION: {"resource_id": "gas"},
    }
    coordinator._cost_breakdowns["electricity"] = CostBreakdown(
        day="2026-09-08",
        total_pence=80.0,
        usage_pence=80.0,
        standing_charge_pence=None,
        standing_charge_status="unknown",
        complete_day=False,
        usage_intervals=(),
    )

    costs, diagnostics = coordinator._compose_costs(
        {
            CLASSIFIER_ELECTRICITY_COST: 80.0,
            CLASSIFIER_GAS_CONSUMPTION: None,
        }
    )

    assert costs["electricity"] == 0.8
    assert costs["electricity_standing_charge"] is None
    assert costs["gas"] is None
    assert costs["standing_charges_total"] is None
    assert diagnostics["gas"]["source"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (GlowmarktAuthError("bad"), "Authentication error"),
        (GlowmarktApiError("down"), "API error"),
    ],
)
async def test_update_data_maps_known_api_failures_to_update_failed(
    hass,
    failure: Exception,
    message: str,
) -> None:
    api = FakeApi()
    api.discover_resources.side_effect = failure
    coordinator = _make(hass, api)

    with pytest.raises(UpdateFailed, match=message):
        await coordinator._async_update_data()


@pytest.mark.asyncio
async def test_shutdown_cancels_only_live_coordinator_tasks(hass) -> None:
    coordinator = _make(hass)
    waiting = asyncio.Event()

    async def wait_forever() -> None:
        await waiting.wait()

    coordinator._history_task = hass.async_create_background_task(
        wait_forever(),
        name="history",
    )
    coordinator._delayed_history_task = hass.async_create_background_task(
        wait_forever(),
        name="delayed",
    )

    await coordinator.async_shutdown()

    assert coordinator._history_task.cancelled()
    assert coordinator._delayed_history_task.cancelled()
