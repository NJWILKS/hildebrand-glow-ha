from __future__ import annotations

from custom_components.hildebrand_glow.costing import CostBreakdown
from custom_components.hildebrand_glow.tariff import (
    derive_tariff_periods,
    normalise_cost_history,
    parse_tariff_periods,
)


def _breakdown(day: str, total: float, pt30m: float) -> CostBreakdown:
    return CostBreakdown(
        day=day,
        total_pence=total,
        usage_pence=pt30m,
        standing_charge_pence=total - pt30m,
        standing_charge_status="applied",
        complete_day=True,
    )


def test_tariff_list_builds_effective_dated_flat_periods() -> None:
    rows = [
        {
            "id": "old",
            "effectiveDate": "2026-04-01 00:00:00",
            "plan": [{"planDetail": [{"rate": 23.10}, {"standing": 55.20}]}],
        },
        {
            "id": "current",
            "from": "2026-07-01 00:00:00",
            "plan": [{"planDetail": [{"rate": "24.50"}, {"standing": 58.20}]}],
        },
    ]

    periods = parse_tariff_periods(rows)

    assert len(periods) == 2
    assert periods[0].effective_from.isoformat() == "2026-04-01"
    assert periods[0].effective_to.isoformat() == "2026-07-01"
    assert periods[0].standing_pence == 55.20
    assert periods[0].unit_rate_pence_per_kwh == 23.10
    assert periods[0].rate_kind == "flat"
    assert periods[1].effective_from.isoformat() == "2026-07-01"
    assert periods[1].effective_to is None
    assert periods[1].standing_pence == 58.20
    assert periods[1].unit_rate_pence_per_kwh == 24.50


def test_tariff_list_keeps_standing_charge_for_tou_tariff() -> None:
    rows = [
        {
            "effectiveDate": "2026-01-01 00:00:00",
            "plan": [
                {
                    "planDetail": [
                        {"standing": 50.0},
                        {"tier": 1, "rate": 30.0, "time": "05:00-23:59"},
                        {"tier": 2, "tourate": 8.0, "time": "00:00-05:00"},
                    ]
                }
            ],
        }
    ]

    period = parse_tariff_periods(rows)[0]

    assert period.standing_pence == 50.0
    assert period.unit_rate_pence_per_kwh is None
    assert period.rate_kind == "tou"


def test_known_current_tariff_calibrates_without_using_noisy_daily_residuals() -> None:
    history = [
        _breakdown("2026-08-01", 558.0, 500.0),
        _breakdown("2026-08-02", 560.0, 501.0),
        _breakdown("2026-08-03", 557.0, 500.0),
        _breakdown("2026-08-04", 559.5, 501.0),
    ]
    rows = [
        {
            "effectiveDate": "2026-07-01 00:00:00",
            "plan": [{"planDetail": [{"rate": 24.50}, {"standing": 58.20}]}],
        }
    ]

    period = derive_tariff_periods(
        rows,
        history,
        configured_standing_gbp=0.582,
        configured_rate_gbp_per_kwh=0.245,
    )[0]

    assert period.standing_pence == 58.20
    assert period.standing_source == "tariff_list"
    assert period.residual_median_pence == 58.25
    assert period.sample_days == 4
    assert period.configured_standing_delta_pence == 0.0
    assert period.unit_rate_pence_per_kwh == 24.50
    assert period.configured_unit_rate_delta_pence == 0.0


def test_missing_historical_standing_is_inferred_per_tariff_period() -> None:
    history = [
        _breakdown("2026-04-01", 349.0, 300.0),
        _breakdown("2026-04-02", 351.0, 300.0),
        _breakdown("2026-04-03", 350.0, 300.0),
        _breakdown("2026-07-01", 458.0, 400.0),
        _breakdown("2026-07-02", 459.0, 400.0),
        _breakdown("2026-07-03", 457.0, 400.0),
    ]
    rows = [
        {"effectiveDate": "2026-04-01 00:00:00", "plan": []},
        {"effectiveDate": "2026-07-01 00:00:00", "plan": []},
    ]

    periods = derive_tariff_periods(rows, history)

    assert periods[0].standing_pence == 50.0
    assert periods[0].standing_source == "inferred_residual_median"
    assert periods[1].standing_pence == 58.0
    assert periods[1].standing_source == "inferred_residual_median"


def test_configured_current_standing_supplies_exact_value_when_cluster_agrees() -> None:
    history = [
        _breakdown("2026-08-01", 558.0, 500.0),
        _breakdown("2026-08-02", 559.0, 500.0),
        _breakdown("2026-08-03", 557.0, 500.0),
    ]
    rows = [{"effectiveDate": "2026-07-01 00:00:00", "plan": []}]

    period = derive_tariff_periods(
        rows,
        history,
        configured_standing_gbp=0.582,
        configured_rate_gbp_per_kwh=0.245,
    )[0]

    assert period.residual_median_pence == 58.0
    assert period.standing_pence == 58.2
    assert period.standing_source == "configured_current_calibrated"
    assert period.unit_rate_pence_per_kwh == 24.5
    assert period.unit_rate_source == "configured_current_fallback"


def test_normalised_components_are_stable_and_reconcile_to_p1d() -> None:
    history = [
        _breakdown("2026-08-01", 558.0, 500.0),
        _breakdown("2026-08-02", 560.0, 501.0),
        _breakdown("2026-08-03", 557.0, 500.0),
    ]
    rows = [
        {
            "effectiveDate": "2026-07-01 00:00:00",
            "plan": [{"planDetail": [{"standing": 58.2}, {"rate": 24.5}]}],
        }
    ]
    periods = derive_tariff_periods(rows, history)

    normalised = normalise_cost_history(history, periods)

    assert [item.standing_charge_pence for item in normalised] == [58.2, 58.2, 58.2]
    assert [item.usage_pence for item in normalised] == [499.8, 501.8, 498.8]
    for item in normalised:
        assert item.usage_pence is not None
        assert item.standing_charge_pence is not None
        assert round(item.usage_pence + item.standing_charge_pence, 6) == round(
            item.total_pence, 6
        )
