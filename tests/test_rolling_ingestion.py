from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from custom_components.hildebrand_glow.api import UK_TZ, DailyReading
from custom_components.hildebrand_glow.const import CLASSIFIER_ELECTRICITY_CONSUMPTION
from custom_components.hildebrand_glow.coordinator import (
    CUMULATIVE_BACKFILL_SCHEMA_VERSION,
    GlowmarktDataUpdateCoordinator,
)


class FakeStore:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    async def async_load(self) -> dict[str, Any]:
        return deepcopy(self.state)

    async def async_save(self, data: dict[str, Any]) -> None:
        self.state.clear()
        self.state.update(deepcopy(data))


class FakeApi:
    def __init__(self, readings: dict[str, DailyReading | None]) -> None:
        self.readings = readings

    async def _fetch_day_reading(
        self,
        _resource_id: str,
        day_start: datetime,
        _day_end: datetime,
        _days_back: int | None = None,
    ) -> DailyReading | None:
        return self.readings.get(day_start.astimezone(UK_TZ).date().isoformat())


def _coordinator(hass, api: FakeApi, state: dict[str, Any]):
    coordinator = GlowmarktDataUpdateCoordinator(
        hass,
        api,  # type: ignore[arg-type]
        {
            "electricity_rate": 0.25,
            "electricity_standing_charge": 0.50,
            "gas_rate": 0.07,
            "gas_standing_charge": 0.30,
        },
    )
    coordinator._resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "electricity-resource"}
    }
    coordinator._store = FakeStore(state)
    coordinator._entity_id_for = lambda _classifier: "sensor.electricity_consumption"
    return coordinator


def _install_stat_writer(coordinator, calls: list[tuple[str, str, float]] | None = None):
    """Replace Recorder I/O while retaining the production baseline contract."""

    def write(classifier: str, reading: DailyReading, baseline: float) -> float:
        if calls is not None:
            calls.append((classifier, reading.day, baseline))
        return round(baseline + reading.value, 3)

    coordinator._add_consumption_statistics = write


def _utc(local: datetime) -> datetime:
    return local.astimezone(timezone.utc)


def _partial_day(day: date, values: list[float]) -> DailyReading:
    start = datetime.combine(day, datetime.min.time(), tzinfo=UK_TZ)
    intervals = [
        (_utc(start + timedelta(minutes=30 * index)), value)
        for index, value in enumerate(values)
    ]
    return DailyReading(
        day=day.isoformat(),
        value=round(sum(values), 3),
        intervals=intervals,
    )


def _complete_day(day: date, value: float = 0.1) -> DailyReading:
    count = GlowmarktDataUpdateCoordinator._expected_intervals(day)
    return _partial_day(day, [value] * count)


def _ready_state(completed_day: str, cumulative: float) -> dict[str, Any]:
    return {
        "_backfilled": True,
        "_backfilled_version": CUMULATIVE_BACKFILL_SCHEMA_VERSION,
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {
            "day": completed_day,
            "completed_day": completed_day,
            "completed_cumulative": cumulative,
            "cumulative": cumulative,
            "live_day": None,
            "live_intervals": 0,
        },
    }


def test_expected_interval_count_is_dst_safe() -> None:
    assert GlowmarktDataUpdateCoordinator._expected_intervals(date(2026, 9, 8)) == 48
    assert GlowmarktDataUpdateCoordinator._expected_intervals(date(2026, 3, 29)) == 46
    assert GlowmarktDataUpdateCoordinator._expected_intervals(date(2025, 10, 26)) == 50


@pytest.mark.asyncio
async def test_open_day_replays_full_snapshot_from_closed_baseline_idempotently(hass) -> None:
    today = date(2026, 9, 8)
    api = FakeApi({today.isoformat(): _partial_day(today, [0.2, 0.3])})
    state = _ready_state("2026-09-07", 100.0)
    coordinator = _coordinator(hass, api, state)
    calls: list[tuple[str, str, float]] = []
    _install_stat_writer(coordinator, calls)

    now = datetime(2026, 9, 8, 1, 10, tzinfo=UK_TZ)
    first = await coordinator._refresh_consumption(now)
    second = await coordinator._refresh_consumption(now)

    assert first[CLASSIFIER_ELECTRICITY_CONSUMPTION] == 100.5
    assert second[CLASSIFIER_ELECTRICITY_CONSUMPTION] == 100.5
    assert [baseline for _classifier, _day, baseline in calls] == [100.0, 100.0]
    entry = state[CLASSIFIER_ELECTRICITY_CONSUMPTION]
    assert entry["completed_cumulative"] == 100.0
    assert entry["cumulative"] == 100.5
    assert entry["live_day"] == "2026-09-08"
    assert entry["live_intervals"] == 2


@pytest.mark.asyncio
async def test_shorter_open_day_response_preserves_last_good_state(hass) -> None:
    today = date(2026, 9, 8)
    api = FakeApi({today.isoformat(): _partial_day(today, [0.2, 0.3])})
    state = _ready_state("2026-09-07", 100.0)
    coordinator = _coordinator(hass, api, state)
    calls: list[tuple[str, str, float]] = []
    _install_stat_writer(coordinator, calls)
    now = datetime(2026, 9, 8, 1, 10, tzinfo=UK_TZ)

    assert (
        await coordinator._refresh_consumption(now)
    )[CLASSIFIER_ELECTRICITY_CONSUMPTION] == 100.5

    api.readings[today.isoformat()] = _partial_day(today, [0.2])
    assert (
        await coordinator._refresh_consumption(now)
    )[CLASSIFIER_ELECTRICITY_CONSUMPTION] == 100.5

    api.readings[today.isoformat()] = None
    assert (
        await coordinator._refresh_consumption(now)
    )[CLASSIFIER_ELECTRICITY_CONSUMPTION] == 100.5
    assert len(calls) == 1
    assert state[CLASSIFIER_ELECTRICITY_CONSUMPTION]["live_intervals"] == 2


@pytest.mark.asyncio
async def test_restart_rebuilds_open_day_from_same_closed_baseline(hass) -> None:
    today = date(2026, 9, 8)
    api = FakeApi({today.isoformat(): _partial_day(today, [0.2, 0.3])})
    state = _ready_state("2026-09-07", 100.0)
    now = datetime(2026, 9, 8, 1, 10, tzinfo=UK_TZ)

    first = _coordinator(hass, api, state)
    _install_stat_writer(first)
    assert (
        await first._refresh_consumption(now)
    )[CLASSIFIER_ELECTRICITY_CONSUMPTION] == 100.5

    restarted = _coordinator(hass, api, state)
    calls: list[tuple[str, str, float]] = []
    _install_stat_writer(restarted, calls)
    assert (
        await restarted._refresh_consumption(now)
    )[CLASSIFIER_ELECTRICITY_CONSUMPTION] == 100.5
    assert calls == [(CLASSIFIER_ELECTRICITY_CONSUMPTION, "2026-09-08", 100.0)]
    assert state[CLASSIFIER_ELECTRICITY_CONSUMPTION]["completed_cumulative"] == 100.0


@pytest.mark.asyncio
async def test_completed_gap_is_reconciled_before_open_day(hass) -> None:
    yesterday = date(2026, 9, 7)
    today = date(2026, 9, 8)
    api = FakeApi(
        {
            yesterday.isoformat(): _complete_day(yesterday, 0.1),
            today.isoformat(): _partial_day(today, [0.2, 0.3]),
        }
    )
    state = _ready_state("2026-09-06", 90.0)
    coordinator = _coordinator(hass, api, state)
    calls: list[tuple[str, str, float]] = []
    _install_stat_writer(coordinator, calls)

    now = datetime(2026, 9, 8, 1, 10, tzinfo=UK_TZ)
    result = await coordinator._refresh_consumption(now)

    assert result[CLASSIFIER_ELECTRICITY_CONSUMPTION] == 95.3
    assert state[CLASSIFIER_ELECTRICITY_CONSUMPTION]["completed_day"] == "2026-09-07"
    assert state[CLASSIFIER_ELECTRICITY_CONSUMPTION]["completed_cumulative"] == 94.8
    assert state[CLASSIFIER_ELECTRICITY_CONSUMPTION]["live_intervals"] == 2
    assert calls == [
        (CLASSIFIER_ELECTRICITY_CONSUMPTION, "2026-09-07", 90.0),
        (CLASSIFIER_ELECTRICITY_CONSUMPTION, "2026-09-08", 94.8),
    ]


@pytest.mark.asyncio
async def test_incomplete_closed_day_holds_previous_live_total(hass) -> None:
    yesterday = date(2026, 9, 7)
    today = date(2026, 9, 8)
    api = FakeApi(
        {
            yesterday.isoformat(): _partial_day(yesterday, [0.1] * 47),
            today.isoformat(): _partial_day(today, [0.2, 0.3]),
        }
    )
    state = _ready_state("2026-09-06", 90.0)
    state[CLASSIFIER_ELECTRICITY_CONSUMPTION].update(
        {
            "cumulative": 94.7,
            "live_day": "2026-09-07",
            "live_intervals": 47,
        }
    )
    coordinator = _coordinator(hass, api, state)
    calls: list[tuple[str, str, float]] = []
    _install_stat_writer(coordinator, calls)

    result = await coordinator._refresh_consumption(
        datetime(2026, 9, 8, 1, 10, tzinfo=UK_TZ)
    )

    assert result[CLASSIFIER_ELECTRICITY_CONSUMPTION] == 94.7
    assert state[CLASSIFIER_ELECTRICITY_CONSUMPTION]["completed_day"] == "2026-09-06"
    assert calls == []
