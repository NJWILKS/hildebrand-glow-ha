from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from custom_components.hildebrand_glow.api import UK_TZ
from custom_components.hildebrand_glow.costing import (
    CostBreakdown,
    _completed_breakdown,
    get_latest_cost_breakdown,
)


class FakeApi:
    def __init__(self, responses: list[list[list[Any]]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, datetime, datetime, str]] = []

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


def test_completed_day_applied_standing_charge_is_p1d_minus_pt30m() -> None:
    assert _completed_breakdown(
        day="2026-09-05",
        pt30m_pence=250.0,
        p1d_pence=297.2,
    ) == CostBreakdown(
        day="2026-09-05",
        total_pence=297.2,
        usage_pence=250.0,
        standing_charge_pence=47.2,
        standing_charge_status="applied",
        complete_day=True,
    )


def test_completed_day_without_daily_bucket_is_pending_not_guessed() -> None:
    result = _completed_breakdown(
        day="2026-09-05",
        pt30m_pence=250.0,
        p1d_pence=None,
    )

    assert result is not None
    assert result.total_pence == 250.0
    assert result.standing_charge_pence is None
    assert result.standing_charge_status == "daily_pending"


def test_negative_residual_is_unknown_not_negative_charge() -> None:
    result = _completed_breakdown(
        day="2026-09-05",
        pt30m_pence=250.0,
        p1d_pence=245.0,
    )

    assert result is not None
    assert result.total_pence == 245.0
    assert result.standing_charge_pence is None
    assert result.standing_charge_status == "unknown"


@pytest.mark.asyncio
async def test_partial_day_uses_pt30m_and_has_zero_standing_charge() -> None:
    now = datetime(2026, 9, 7, 12, 0, tzinfo=UK_TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    api = FakeApi(
        [
            [
                [_epoch(today), 20.0],
                [_epoch(today.replace(minute=30)), 30.0],
            ]
        ]
    )

    result = await get_latest_cost_breakdown(
        api,  # type: ignore[arg-type]
        "electricity-cost",
        now_uk=now,
    )

    assert result == CostBreakdown(
        day="2026-09-07",
        total_pence=50.0,
        usage_pence=50.0,
        standing_charge_pence=0.0,
        standing_charge_status="not_applied",
        complete_day=False,
    )
    assert [call[3] for call in api.calls] == ["PT30M"]


@pytest.mark.asyncio
async def test_completed_day_compares_pt30m_with_p1d() -> None:
    now = datetime(2026, 9, 7, 0, 0, tzinfo=UK_TZ)
    day_start = datetime(2026, 9, 6, 0, 0, tzinfo=UK_TZ)
    api = FakeApi(
        [
            [
                [_epoch(day_start), 100.0],
                [_epoch(day_start.replace(hour=12)), 150.0],
            ],
            [[_epoch(day_start), 297.2]],
        ]
    )

    result = await get_latest_cost_breakdown(
        api,  # type: ignore[arg-type]
        "electricity-cost",
        now_uk=now,
    )

    assert result is not None
    assert result.total_pence == 297.2
    assert result.usage_pence == 250.0
    assert result.standing_charge_pence == 47.2
    assert result.standing_charge_status == "applied"
    assert [call[3] for call in api.calls] == ["PT30M", "P1D"]


@pytest.mark.asyncio
async def test_missing_today_falls_back_to_latest_completed_day() -> None:
    now = datetime(2026, 9, 7, 12, 0, tzinfo=UK_TZ)
    yesterday = datetime(2026, 9, 6, 0, 0, tzinfo=UK_TZ)
    api = FakeApi(
        [
            [],
            [[_epoch(yesterday), 250.0]],
            [[_epoch(yesterday), 297.2]],
        ]
    )

    result = await get_latest_cost_breakdown(
        api,  # type: ignore[arg-type]
        "electricity-cost",
        now_uk=now,
    )

    assert result is not None
    assert result.day == "2026-09-06"
    assert result.complete_day is True
    assert result.standing_charge_pence == 47.2
