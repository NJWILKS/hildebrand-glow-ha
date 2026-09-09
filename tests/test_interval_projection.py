from __future__ import annotations

from datetime import date

from custom_components.hildebrand_glow.interval_projection import (
    DailyCostProjection,
    build_cost_stack_statistics,
    build_daily_cost_projection,
)


def _period(
    effective_from: str,
    *,
    effective_to: str | None = None,
    standing_pence: float | None = 50.0,
) -> dict:
    return {
        "effective_from": effective_from,
        "effective_to": effective_to,
        "standing_pence": standing_pence,
        "standing_source": "tariff_list" if standing_pence is not None else "unavailable",
    }


def test_daily_stack_uses_glow_pt30m_cost_and_one_standing_charge() -> None:
    intervals = [
        {
            "timestamp": "2026-01-02T00:00:00+00:00",
            "usage_kwh": 0.2,
            "cost_pence": 4.5,
        },
        {
            "timestamp": "2026-01-02T00:30:00+00:00",
            "usage_kwh": 0.3,
            "cost_pence": 6.5,
        },
    ]

    daily = build_daily_cost_projection(intervals, [_period("2026-01-01")])

    assert daily == [
        DailyCostProjection(
            day=date(2026, 1, 2),
            usage_pence=11.0,
            standing_pence=50.0,
            total_pence=61.0,
            standing_source="tariff_list",
        )
    ]

    usage, standing, total = build_cost_stack_statistics(daily)
    assert usage[0]["state"] == 0.11
    assert usage[0]["sum"] == 0.11
    assert standing[0]["state"] == 0.5
    assert standing[0]["sum"] == 0.5
    assert total[0]["state"] == 0.61
    assert total[0]["sum"] == 0.61


def test_tariff_transition_changes_standing_component_by_effective_day() -> None:
    intervals = [
        {"timestamp": "2026-03-31T12:00:00+00:00", "cost_pence": 20.0},
        {"timestamp": "2026-04-01T12:00:00+00:00", "cost_pence": 30.0},
    ]
    periods = [
        _period("2026-01-01", effective_to="2026-04-01", standing_pence=45.0),
        _period("2026-04-01", standing_pence=55.0),
    ]

    daily = build_daily_cost_projection(intervals, periods)

    assert [item.standing_pence for item in daily] == [45.0, 55.0]
    assert [item.total_pence for item in daily] == [65.0, 85.0]


def test_autumn_dst_day_applies_standing_once() -> None:
    # Both UTC timestamps map into the repeated 01:30 local clock hour on the
    # autumn transition day.  They remain separate raw intervals but one billing day.
    intervals = [
        {"timestamp": "2026-10-25T00:30:00+00:00", "cost_pence": 4.0},
        {"timestamp": "2026-10-25T01:30:00+00:00", "cost_pence": 5.0},
    ]

    daily = build_daily_cost_projection(intervals, [_period("2026-10-01", standing_pence=60.0)])

    assert len(daily) == 1
    assert daily[0].usage_pence == 9.0
    assert daily[0].standing_pence == 60.0
    assert daily[0].total_pence == 69.0


def test_missing_standing_keeps_usage_but_does_not_invent_total() -> None:
    intervals = [
        {"timestamp": "2026-05-01T12:00:00+00:00", "cost_pence": 12.5},
    ]

    daily = build_daily_cost_projection(
        intervals,
        [_period("2026-05-01", standing_pence=None)],
    )

    assert daily[0].usage_pence == 12.5
    assert daily[0].standing_pence is None
    assert daily[0].total_pence is None

    usage, standing, total = build_cost_stack_statistics(daily)
    assert usage[0]["state"] == 0.125
    assert standing == []
    assert total == []


def test_zero_cost_is_real_data_not_missing_data() -> None:
    daily = build_daily_cost_projection(
        [{"timestamp": "2026-06-01T12:00:00+00:00", "cost_pence": 0.0}],
        [_period("2026-06-01", standing_pence=40.0)],
    )

    assert daily[0].usage_pence == 0.0
    assert daily[0].standing_pence == 40.0
    assert daily[0].total_pence == 40.0


def test_missing_pt30m_cost_keeps_standing_visible_but_total_unknown() -> None:
    daily = build_daily_cost_projection(
        [{"timestamp": "2026-07-01T12:00:00+00:00", "usage_kwh": 0.4, "cost_pence": None}],
        [_period("2026-07-01", standing_pence=42.0)],
    )

    assert daily[0].usage_pence is None
    assert daily[0].standing_pence == 42.0
    assert daily[0].total_pence is None

    usage, standing, total = build_cost_stack_statistics(daily)
    assert usage == []
    assert standing[0]["state"] == 0.42
    assert total == []


def test_statistics_use_uk_local_midnight_and_monotonic_sums() -> None:
    daily = [
        DailyCostProjection(date(2026, 7, 1), 10.0, 40.0, 50.0, "tariff_list"),
        DailyCostProjection(date(2026, 7, 2), 20.0, 40.0, 60.0, "tariff_list"),
    ]

    usage, standing, total = build_cost_stack_statistics(daily)

    # July is BST, so UK-local midnight is 23:00 UTC on the previous date.
    assert usage[0]["start"].isoformat() == "2026-06-30T23:00:00+00:00"
    assert [row["sum"] for row in usage] == [0.1, 0.3]
    assert [row["sum"] for row in standing] == [0.4, 0.8]
    assert [row["sum"] for row in total] == [0.5, 1.1]
