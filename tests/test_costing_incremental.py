from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from custom_components.hildebrand_glow.api import UK_TZ
from custom_components.hildebrand_glow.costing import get_cost_history


class IncrementalFakeApi:
    def __init__(self, pt30m_rows: list[list[Any]], p1d_rows: list[list[Any]]) -> None:
        self.pt30m_rows = pt30m_rows
        self.p1d_rows = p1d_rows
        self.calls: list[tuple[datetime, datetime, str]] = []

    async def get_first_available_reading_time(self, resource_id: str) -> datetime:
        raise AssertionError("incremental history must not probe first-time")

    async def _request_readings(
        self,
        resource_id: str,
        start: datetime,
        end: datetime,
        period: str,
    ) -> list[list[Any]]:
        self.calls.append((start, end, period))
        return self.pt30m_rows if period == "PT30M" else self.p1d_rows


def _epoch(value: datetime) -> int:
    return int(value.timestamp())


@pytest.mark.asyncio
async def test_cost_history_can_extend_from_known_day_without_full_rescan() -> None:
    start = datetime(2026, 9, 5, 0, 0, tzinfo=UK_TZ)
    next_day = datetime(2026, 9, 6, 0, 0, tzinfo=UK_TZ)
    now = datetime(2026, 9, 7, 12, 0, tzinfo=UK_TZ)
    api = IncrementalFakeApi(
        [
            [_epoch(start), 100.0],
            [_epoch(next_day), 150.0],
        ],
        [
            [_epoch(start), 147.2],
            [_epoch(next_day), 197.2],
        ],
    )

    history = await get_cost_history(
        api,  # type: ignore[arg-type]
        "electricity-cost",
        now_uk=now,
        start_uk=start,
    )

    assert [item.day for item in history] == ["2026-09-05", "2026-09-06"]
    assert [item.standing_charge_pence for item in history] == [47.2, 47.2]
    assert [period for _, _, period in api.calls] == ["PT30M", "P1D"]
