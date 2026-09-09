from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_recorder_block_till_done,
)

from custom_components.hildebrand_glow.api import DailyReading
from custom_components.hildebrand_glow.consumption_statistics import (
    add_consumption_statistics,
    energy_consumption_statistic_id,
)
from custom_components.hildebrand_glow.cost_ingestion import (
    _external_component_metadata,
    build_component_statistics,
    cost_component_statistic_id,
)
from custom_components.hildebrand_glow.costing import CostBreakdown


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url: str) -> None:
    """Use the real test Recorder rather than the plugin's pre-hass recorder mock."""
    assert recorder_db_url


def _reading(day: str, values: list[float]) -> DailyReading:
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return DailyReading(
        day=day,
        value=sum(values),
        intervals=[
            (start + timedelta(minutes=30 * index), value)
            for index, value in enumerate(values)
        ],
    )


async def _clear(hass, statistic_ids: list[str]) -> None:
    done = asyncio.Event()

    def complete() -> None:
        hass.loop.call_soon_threadsafe(done.set)

    get_instance(hass).async_clear_statistics(statistic_ids, on_done=complete)
    await done.wait()
    await async_recorder_block_till_done(hass)


async def _stats(
    hass,
    statistic_id: str,
    start: datetime,
    end: datetime,
    *,
    period: str = "hour",
    types: set[str] | None = None,
) -> list[dict]:
    result = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        period,
        None,
        types or {"state", "sum"},
    )
    return result.get(statistic_id, [])


@pytest.mark.asyncio
async def test_reset_rebuild_cannot_create_negative_energy_delta(
    hass,
    recorder_mock,
) -> None:
    """Reproduce the 2.3.3 failure shape against a real Recorder database.

    The old implementation cleared a sensor-owned TOTAL_INCREASING statistic while
    Recorder also owned the live state, which could produce a -lifetime-total hour.
    2.3.4 has one external writer for both historical and open-day rows. Clearing
    and rebuilding the entire series mid-day must leave every Energy sum delta >= 0.
    """
    statistic_id = energy_consumption_statistic_id("site-123", "electricity")
    closed = _reading("2026-09-07", [1.0] * 48)
    open_day = _reading("2026-09-08", [0.2, 0.3, 0.4, 0.1])

    _closed_stats, baseline = add_consumption_statistics(
        hass,
        "site-123",
        "electricity",
        [closed],
    )
    add_consumption_statistics(
        hass,
        "site-123",
        "electricity",
        [open_day],
        baseline=baseline,
    )
    await async_recorder_block_till_done(hass)

    await _clear(hass, [statistic_id])

    _closed_stats, baseline = add_consumption_statistics(
        hass,
        "site-123",
        "electricity",
        [closed],
    )
    add_consumption_statistics(
        hass,
        "site-123",
        "electricity",
        [open_day],
        baseline=baseline,
    )
    await async_recorder_block_till_done(hass)

    rows = await _stats(
        hass,
        statistic_id,
        datetime(2026, 9, 7, tzinfo=timezone.utc),
        datetime(2026, 9, 9, tzinfo=timezone.utc),
    )

    assert rows
    sums = [float(row["sum"]) for row in rows]
    states = [float(row["state"]) for row in rows]
    assert all(value >= 0 for value in states)
    assert all(value >= 0 for value in sums)
    assert all(later >= earlier for earlier, later in zip(sums, sums[1:], strict=False))
    assert round(sums[-1], 6) == 49.0
    assert max(states) < 10.0


@pytest.mark.asyncio
async def test_real_recorder_persists_external_stackable_cost_components(
    hass,
    recorder_mock,
) -> None:
    """External usage/standing stats persist and produce additive daily changes."""
    start = datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc)
    breakdown = CostBreakdown(
        day="2026-09-08",
        total_pence=107.2,
        usage_pence=49.0,
        standing_charge_pence=58.2,
        standing_charge_status="tariff_list",
        complete_day=False,
        usage_intervals=((start, 24.5), (start + timedelta(minutes=30), 24.5)),
    )
    usage_stats, standing_stats = build_component_statistics([breakdown])
    usage_id = cost_component_statistic_id("site-123", "electricity", "usage_cost")
    standing_id = cost_component_statistic_id(
        "site-123", "electricity", "standing_charge"
    )

    async_add_external_statistics(
        hass,
        _external_component_metadata("site-123", "electricity", "usage_cost"),
        usage_stats,
    )
    async_add_external_statistics(
        hass,
        _external_component_metadata("site-123", "electricity", "standing_charge"),
        standing_stats,
    )
    await async_recorder_block_till_done(hass)

    end = start + timedelta(days=1)
    usage_rows = await _stats(hass, usage_id, start, end)
    standing_rows = await _stats(hass, standing_id, start, end)

    assert usage_rows
    assert standing_rows
    assert round(float(usage_rows[-1]["state"]), 6) == 0.49
    assert round(float(standing_rows[-1]["state"]), 6) == 0.582

    usage_daily = await _stats(
        hass,
        usage_id,
        start,
        end,
        period="day",
        types={"change"},
    )
    standing_daily = await _stats(
        hass,
        standing_id,
        start,
        end,
        period="day",
        types={"change"},
    )

    assert round(float(usage_daily[-1]["change"]), 6) == 0.49
    assert round(float(standing_daily[-1]["change"]), 6) == 0.582
    assert round(
        float(usage_daily[-1]["change"]) + float(standing_daily[-1]["change"]),
        6,
    ) == 1.072
