from __future__ import annotations

from datetime import datetime, timezone

from custom_components.hildebrand_glow.api import UK_TZ
from custom_components.hildebrand_glow.cost_history import (
    _effective_key,
    _external_resume_point,
    build_component_statistics,
    build_total_cost_statistics,
    energy_cost_statistic_id,
)
from custom_components.hildebrand_glow.costing import CostBreakdown


def test_cost_component_statistics_are_daily_stackable_states() -> None:
    history = [
        CostBreakdown(
            day="2025-10-26",
            total_pence=347.2,
            usage_pence=300.0,
            standing_charge_pence=47.2,
            standing_charge_status="applied",
            complete_day=True,
        ),
        CostBreakdown(
            day="2025-10-27",
            total_pence=250.0,
            usage_pence=250.0,
            standing_charge_pence=0.0,
            standing_charge_status="not_applied",
            complete_day=True,
        ),
    ]

    usage, standing = build_component_statistics(history)

    assert [item["state"] for item in usage] == [3.0, 2.5]
    assert [item["sum"] for item in usage] == [3.0, 5.5]
    assert [item["state"] for item in standing] == [0.472, 0.0]
    assert [item["sum"] for item in standing] == [0.472, 0.472]
    assert usage[0]["start"] == datetime(
        2025,
        10,
        25,
        23,
        0,
        tzinfo=timezone.utc,
    )


def test_total_cost_statistics_are_cumulative_for_energy_dashboard() -> None:
    first_hour = datetime(2025, 10, 25, 23, 0, tzinfo=timezone.utc)
    second_hour = datetime(2025, 10, 26, 0, 0, tzinfo=timezone.utc)
    history = [
        CostBreakdown(
            day="2025-10-26",
            total_pence=347.2,
            usage_pence=300.0,
            standing_charge_pence=47.2,
            standing_charge_status="applied",
            complete_day=True,
            usage_intervals=(
                (first_hour, 100.0),
                (second_hour, 200.0),
            ),
        ),
        CostBreakdown(
            day="2025-10-27",
            total_pence=250.0,
            usage_pence=250.0,
            standing_charge_pence=0.0,
            standing_charge_status="not_applied",
            complete_day=True,
        ),
    ]

    total = build_total_cost_statistics(history)

    assert [item["state"] for item in total] == [1.472, 2.0, 2.5]
    assert [item["sum"] for item in total] == [1.472, 3.472, 5.972]
    assert [item["start"] for item in total[:2]] == [first_hour, second_hour]
    assert total[2]["start"] == datetime(
        2025,
        10,
        27,
        0,
        0,
        tzinfo=timezone.utc,
    )


def test_cost_statistics_preserve_sub_penny_precision() -> None:
    hour = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)
    history = [
        CostBreakdown(
            day="2026-09-05",
            total_pence=147.619,
            usage_pence=100.010,
            standing_charge_pence=47.609,
            standing_charge_status="applied",
            complete_day=True,
            usage_intervals=((hour, 100.010),),
        )
    ]

    total = build_total_cost_statistics(history)
    usage, standing = build_component_statistics(history)

    assert total[0]["state"] == 1.47619
    assert total[0]["sum"] == 1.47619
    assert usage[0]["state"] == 1.0001
    assert standing[0]["state"] == 0.47609
    assert usage[0]["state"] + standing[0]["state"] == total[0]["state"]


def test_energy_cost_statistic_id_is_stable_and_valid() -> None:
    assert (
        energy_cost_statistic_id("ABC-123/site", "Electricity")
        == "hildebrand_glow:abc_123_site_electricity_energy_cost"
    )


def test_external_cost_resume_uses_recorder_sum_and_next_uk_day() -> None:
    last_hour = datetime(2026, 9, 5, 22, 0, tzinfo=timezone.utc)

    start_uk, baseline = _external_resume_point(
        {
            "start": last_hour.timestamp(),
            "sum": 718.40123449,
        }
    )

    assert start_uk == datetime(2026, 9, 6, 0, 0, tzinfo=UK_TZ)
    assert baseline == 718.401234


def test_external_cost_resume_without_recorder_row_forces_full_history() -> None:
    start_uk, baseline = _external_resume_point(None)

    assert start_uk is None
    assert baseline == 0.0


def test_unknown_standing_charge_does_not_invent_historical_value() -> None:
    history = [
        CostBreakdown(
            day="2026-03-29",
            total_pence=300.0,
            usage_pence=250.0,
            standing_charge_pence=None,
            standing_charge_status="unknown",
            complete_day=True,
        )
    ]

    usage, standing = build_component_statistics(history)

    assert len(usage) == 1
    assert usage[0]["start"] == datetime(
        2026,
        3,
        29,
        0,
        0,
        tzinfo=timezone.utc,
    )
    assert standing == []


def test_tariff_history_sort_key_prefers_effective_date_then_from() -> None:
    rows = [
        {"from": "2026-07-01 00:00:00"},
        {"effectiveDate": "2026-04-01 00:00:00"},
        {"effectiveDate": "2026-10-01 00:00:00"},
    ]

    assert sorted(rows, key=_effective_key) == [rows[1], rows[0], rows[2]]
