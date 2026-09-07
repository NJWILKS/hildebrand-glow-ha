from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from custom_components.hildebrand_glow.api import UK_TZ
from custom_components.hildebrand_glow.costing import get_cost_history


class FakeHistoryApi:
    def __init__(self, responses: list[list[list[Any]]], first: datetime) -> None:
        self._responses = list(responses)
        self._first = first
        self.calls: list[tuple[str, datetime, datetime, str]] = []

    async def get_first_available_reading_time(self, resource_id: str) -> datetime:
        return self._first

    async def _request_readings(
        self,
        resource_id: str,
        start: datetime,
        end: datetime,
        period: str,
    ) -> list[list[Any]]:
        self.calls.append((resource_id, start, end, period))
        return self._responses.pop(0)


def _epoch(value: datetime) -> int:
    return int(value.timestamp())


@pytest.mark.asyncio
async def test_cost_history_reconciles_daily_total_with_usage_only_intervals() -> None:
    day_one = datetime(2026, 9, 5, 0, 0, tzinfo=UK_TZ)
    day_two = datetime(2026, 9, 6, 0, 0, tzinfo=UK_TZ)
    today = datetime(2026, 9, 7, 12, 0, tzinfo=UK_TZ)
    api = FakeHistoryApi(
        responses=[
            [
                [_epoch(day_one), 100.0],
                [_epoch(day_one.replace(hour=12)), 150.0],
                [_epoch(day_two), 80.0],
                [_epoch(day_two.replace(hour=12)), 120.0],
            ],
            [
                [_epoch(day_one), 297.2],
                [_epoch(day_two), 247.2],
            ],
        ],
        first=day_one,
    )

    history = await get_cost_history(
        api,  # type: ignore[arg-type]
        "electricity-cost",
        now_uk=today,
    )

    assert [item.day for item in history] == ["2026-09-05", "2026-09-06"]
    assert [item.usage_pence for item in history] == [250.0, 200.0]
    assert [item.standing_charge_pence for item in history] == [47.2, 47.2]
    assert [item.total_pence for item in history] == [297.2, 247.2]
    assert [call[3] for call in api.calls] == ["PT30M", "P1D"]


@pytest.mark.asyncio
async def test_cost_history_excludes_current_partial_day() -> None:
    yesterday = datetime(2026, 9, 6, 0, 0, tzinfo=UK_TZ)
    today = datetime(2026, 9, 7, 12, 0, tzinfo=UK_TZ)
    api = FakeHistoryApi(
        responses=[
            [[_epoch(yesterday), 200.0]],
            [[_epoch(yesterday), 247.2]],
        ],
        first=yesterday,
    )

    history = await get_cost_history(
        api,  # type: ignore[arg-type]
        "electricity-cost",
        now_uk=today,
    )

    assert len(history) == 1
    assert history[0].day == "2026-09-06"
