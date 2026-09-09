from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from custom_components.hildebrand_glow.api import HISTORY_INTERVAL_DAYS, UK_TZ
from custom_components.hildebrand_glow.costing import (
    COST_DAILY_INTERVAL_DAYS,
    _completed_breakdown,
    _group_daily_by_day,
    _group_pt30m_by_day,
    _window_intervals,
    _window_total,
    get_cost_history,
    get_latest_cost_breakdown,
)


class FakeApi:
    def __init__(
        self,
        responses: list[list[list[Any]]],
        *,
        first: datetime | None = None,
    ) -> None:
        self.responses = list(responses)
        self.first = first
        self.calls: list[tuple[str, datetime, datetime, str]] = []

    async def get_first_available_reading_time(self, _resource_id: str) -> datetime | None:
        return self.first

    async def _request_readings(
        self,
        resource_id: str,
        start: datetime,
        end: datetime,
        period: str,
    ) -> list[list[Any]]:
        self.calls.append((resource_id, start, end, period))
        return self.responses.pop(0)


def _epoch(value: datetime) -> int:
    return int(value.timestamp())


def test_window_helpers_ignore_null_and_outside_rows_and_sort() -> None:
    start = datetime(2026, 9, 1, tzinfo=UK_TZ)
    end = start + timedelta(days=1)
    rows = [
        [_epoch(start - timedelta(minutes=30)), 99.0],
        [_epoch(start + timedelta(hours=2)), 2.0],
        [_epoch(start + timedelta(hours=1)), 1.0],
        [_epoch(start + timedelta(hours=3)), None],
        [_epoch(end), 99.0],
        [123],
    ]

    intervals = _window_intervals(rows, start, end)
    assert [value for _, value in intervals] == [1.0, 2.0]
    assert _window_total(rows, start, end) == 3.0
    assert _window_total([[123], [_epoch(start), None]], start, end) is None

    grouped = _group_pt30m_by_day(rows, start, end)
    assert list(grouped) == ["2026-09-01"]
    assert [value for _, value in grouped["2026-09-01"]] == [1.0, 2.0]
    assert _group_daily_by_day(
        [[_epoch(start), 10.1234], [_epoch(start + timedelta(hours=12)), 11.9999]],
        start,
        end,
    ) == {"2026-09-01": 12.0}


def test_completed_breakdown_covers_empty_daily_only_and_tolerance() -> None:
    assert _completed_breakdown(day="2026-09-01", pt30m_pence=None, p1d_pence=None) is None

    daily_only = _completed_breakdown(
        day="2026-09-01",
        pt30m_pence=None,
        p1d_pence=100.0,
    )
    assert daily_only is not None
    assert daily_only.usage_pence is None
    assert daily_only.standing_charge_status == "unknown"

    within_tolerance = _completed_breakdown(
        day="2026-09-01",
        pt30m_pence=100.0,
        p1d_pence=100.9,
    )
    assert within_tolerance is not None
    assert within_tolerance.standing_charge_pence == 0.0
    assert within_tolerance.standing_charge_status == "not_applied"


@pytest.mark.asyncio
async def test_latest_cost_returns_none_when_lookback_has_no_data() -> None:
    now = datetime(2026, 9, 7, 0, 0, tzinfo=UK_TZ)
    api = FakeApi([[], [], [], []])

    result = await get_latest_cost_breakdown(
        api,  # type: ignore[arg-type]
        "resource",
        now_uk=now,
        completed_lookback_days=2,
    )

    assert result is None
    assert [call[3] for call in api.calls] == ["PT30M", "P1D", "PT30M", "P1D"]


@pytest.mark.asyncio
async def test_latest_cost_falls_back_to_p1d_when_pt30m_missing() -> None:
    now = datetime(2026, 9, 7, 0, 0, tzinfo=UK_TZ)
    yesterday = now - timedelta(days=1)
    api = FakeApi([[], [[_epoch(yesterday), 120.0]]])

    result = await get_latest_cost_breakdown(
        api,  # type: ignore[arg-type]
        "resource",
        now_uk=now,
        completed_lookback_days=1,
    )

    assert result is not None
    assert result.total_pence == 120.0
    assert result.usage_pence is None
    assert result.standing_charge_status == "unknown"


@pytest.mark.asyncio
async def test_cost_history_returns_empty_without_first_reading_or_for_today_only() -> None:
    now = datetime(2026, 9, 7, 12, 0, tzinfo=UK_TZ)
    missing = FakeApi([], first=None)
    assert await get_cost_history(missing, "resource", now_uk=now) == []

    today = FakeApi([], first=now)
    assert await get_cost_history(today, "resource", now_uk=now) == []


@pytest.mark.asyncio
async def test_cost_history_chunks_usage_and_daily_and_merges_union_of_days() -> None:
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UK_TZ)
    start = datetime(2026, 8, 30, 17, 0, tzinfo=UK_TZ)
    day1 = datetime(2026, 8, 30, tzinfo=UK_TZ)
    day2 = datetime(2026, 9, 2, tzinfo=UK_TZ)
    day3 = datetime(2026, 9, 3, tzinfo=UK_TZ)

    usage_chunks = 2
    daily_chunks = 2
    api = FakeApi(
        [
            [[_epoch(day1), 10.0], [_epoch(day2), 20.0]],
            [],
            [[_epoch(day1), 60.0], [_epoch(day3), 70.0]],
            [],
        ],
        first=start,
    )

    history = await get_cost_history(
        api,  # type: ignore[arg-type]
        "resource",
        now_uk=now,
    )

    assert len(history) == 3
    assert [item.day for item in history] == ["2026-08-30", "2026-09-02", "2026-09-03"]
    assert history[0].standing_charge_pence == 50.0
    assert history[1].standing_charge_status == "daily_pending"
    assert history[2].usage_pence is None
    assert sum(call[3] == "PT30M" for call in api.calls) == usage_chunks
    assert sum(call[3] == "P1D" for call in api.calls) == daily_chunks
    assert api.calls[0][2] - api.calls[0][1] <= timedelta(days=HISTORY_INTERVAL_DAYS)
    first_daily = next(call for call in api.calls if call[3] == "P1D")
    assert first_daily[2] - first_daily[1] <= timedelta(days=COST_DAILY_INTERVAL_DAYS)
