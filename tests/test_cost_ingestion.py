from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.hildebrand_glow.const import DOMAIN
from custom_components.hildebrand_glow.cost_ingestion import (
    _external_component_metadata,
    _settled_prefix,
    build_component_statistics,
    build_total_cost_statistics,
    cost_component_statistic_id,
)
from custom_components.hildebrand_glow.costing import CostBreakdown


def _breakdown(
    *,
    total_pence: float = 110.0,
    usage_pence: float = 60.0,
    standing_pence: float | None = 50.0,
    complete: bool = True,
    intervals: tuple[tuple[datetime, float], ...] | None = None,
    status: str = "applied",
) -> CostBreakdown:
    start = datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc)
    if intervals is None:
        intervals = (
            (start, 10.0),
            (start + timedelta(minutes=30), 20.0),
            (start + timedelta(hours=1), 30.0),
        )
    return CostBreakdown(
        day="2026-09-07",
        total_pence=total_pence,
        usage_pence=usage_pence,
        standing_charge_pence=standing_pence,
        standing_charge_status=status,
        complete_day=complete,
        usage_intervals=intervals,
    )


def test_cost_component_statistics_have_stable_external_ids() -> None:
    usage_id = cost_component_statistic_id("site-123", "electricity", "usage_cost")
    standing_id = cost_component_statistic_id(
        "site-123", "electricity", "standing_charge"
    )

    assert usage_id == "hildebrand_glow:site_123_electricity_usage_cost"
    assert standing_id == "hildebrand_glow:site_123_electricity_standing_charge"

    usage_metadata = _external_component_metadata(
        "site-123", "electricity", "usage_cost"
    )
    standing_metadata = _external_component_metadata(
        "site-123", "electricity", "standing_charge"
    )
    assert usage_metadata["source"] == DOMAIN
    assert standing_metadata["source"] == DOMAIN
    assert usage_metadata["statistic_id"] == usage_id
    assert standing_metadata["statistic_id"] == standing_id


def test_component_statistics_match_live_daily_reset_semantics() -> None:
    usage, standing = build_component_statistics(
        [_breakdown()],
        usage_baseline=10.0,
        standing_baseline=5.0,
    )

    assert [item["state"] for item in usage] == [0.3, 0.6]
    assert [item["sum"] for item in usage] == [10.3, 10.6]
    assert [item["state"] for item in standing] == [0.5, 0.5]
    assert [item["sum"] for item in standing] == [5.5, 5.5]


def test_component_usage_finishes_on_normalised_authoritative_value() -> None:
    usage, _standing = build_component_statistics(
        [_breakdown(usage_pence=59.4, standing_pence=50.6)],
        usage_baseline=2.0,
    )

    assert usage[-1]["state"] == 0.594
    assert usage[-1]["sum"] == 2.594


def test_open_day_total_cost_is_rebuilt_from_full_snapshot() -> None:
    start = datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc)
    first = _breakdown(
        total_pence=30.0,
        usage_pence=30.0,
        standing_pence=0.0,
        complete=False,
        status="not_applied",
        intervals=((start, 10.0), (start + timedelta(minutes=30), 20.0)),
    )
    later = _breakdown(
        total_pence=60.0,
        usage_pence=60.0,
        standing_pence=0.0,
        complete=False,
        status="not_applied",
        intervals=(
            (start, 10.0),
            (start + timedelta(minutes=30), 20.0),
            (start + timedelta(hours=1), 30.0),
        ),
    )

    first_stats = build_total_cost_statistics([first], baseline=10.0)
    later_stats = build_total_cost_statistics([later], baseline=10.0)

    assert [(item["state"], item["sum"]) for item in first_stats] == [(0.3, 10.3)]
    assert [(item["state"], item["sum"]) for item in later_stats] == [
        (0.3, 10.3),
        (0.3, 10.6),
    ]


def test_completed_total_cost_uses_p1d_authority() -> None:
    stats = build_total_cost_statistics([_breakdown()], baseline=10.0)

    # PT30M usage is £0.60, but P1D is £1.10. The residual is applied once and
    # the cumulative external Energy cost finishes at the authoritative total.
    assert sum(item["state"] for item in stats) == 1.1
    assert stats[-1]["sum"] == 11.1


def test_pending_p1d_stops_completed_prefix() -> None:
    complete = _breakdown()
    pending = _breakdown(
        complete=True,
        standing_pence=None,
        status="daily_pending",
    )
    later = _breakdown()

    assert _settled_prefix([complete, pending, later]) == [complete]
