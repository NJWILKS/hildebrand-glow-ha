from __future__ import annotations

from datetime import date

from custom_components.hildebrand_glow.costing import CostBreakdown
from custom_components.hildebrand_glow.tariff import (
    _effective_date,
    _number,
    _tariff_values,
    derive_tariff_periods,
    parse_tariff_periods,
    tariff_period_as_dict,
)


def _breakdown(
    day: str,
    *,
    total: float = 100.0,
    usage: float | None = 40.0,
    complete: bool = True,
) -> CostBreakdown:
    return CostBreakdown(
        day=day,
        total_pence=total,
        usage_pence=usage,
        standing_charge_pence=(total - usage) if usage is not None else None,
        standing_charge_status="applied",
        complete_day=complete,
    )


def test_number_parser_rejects_bool_and_invalid_text() -> None:
    assert _number(True) is None
    assert _number(object()) is None
    assert _number(" 24.50 ") == 24.5
    assert _number("not-a-number") is None


def test_effective_date_accepts_date_and_timezone_and_rejects_garbage() -> None:
    assert _effective_date({}) is None
    assert _effective_date({"effectiveDate": "not-a-date"}) is None
    assert _effective_date({"effectiveDate": "2026-07-01"}) == date(2026, 7, 1)
    assert _effective_date({"effectiveDate": "2026-07-01T00:30:00+02:00"}) == date(
        2026, 6, 30
    )


def test_tariff_value_parser_distinguishes_dynamic_and_unknown_shapes() -> None:
    standing, rate, kind = _tariff_values(
        {
            "plan": [
                {
                    "standing": 50,
                    "rate": 20,
                    "dynamic": "half-hourly",
                }
            ]
        }
    )
    assert standing == 50.0
    assert rate is None
    assert kind == "dynamic"

    standing, rate, kind = _tariff_values(
        {"plan": [{"standingCharge": 40, "rate": 20}, {"rate": 30}]}
    )
    assert standing == 40.0
    assert rate is None
    assert kind == "unknown"


def test_parse_tariff_periods_ignores_invalid_rows_and_prefers_richer_duplicate() -> None:
    rows = [
        {"effectiveDate": "bad", "plan": []},
        {"effectiveDate": "2026-07-01", "plan": []},
        {
            "effectiveDate": "2026-07-01",
            "plan": [{"standing": 58.2, "rate": 24.5}],
        },
    ]

    periods = parse_tariff_periods(rows)

    assert len(periods) == 1
    assert periods[0].standing_pence == 58.2
    assert periods[0].unit_rate_pence_per_kwh == 24.5
    assert periods[0].rate_kind == "flat"


def test_derive_returns_parsed_periods_unchanged_without_history() -> None:
    periods = derive_tariff_periods(
        [{"effectiveDate": "2026-07-01", "plan": [{"rate": 24.5}]}],
        [],
    )

    assert len(periods) == 1
    assert periods[0].effective_from == date(2026, 7, 1)
    assert periods[0].standing_pence is None


def test_derive_creates_unknown_period_when_tariff_history_is_absent() -> None:
    periods = derive_tariff_periods(
        [],
        [
            _breakdown("2026-06-01", total=150.0, usage=100.0),
            _breakdown("2026-06-02", total=151.0, usage=100.0),
            _breakdown("2026-06-03", total=149.0, usage=100.0),
        ],
    )

    assert len(periods) == 1
    assert periods[0].effective_from == date(2026, 6, 1)
    assert periods[0].standing_pence == 50.0
    assert periods[0].standing_source == "inferred_residual_median"


def test_derive_inserts_unknown_prefix_before_first_known_tariff() -> None:
    periods = derive_tariff_periods(
        [
            {
                "effectiveDate": "2026-07-01",
                "plan": [{"standing": 58.2, "rate": 24.5}],
            }
        ],
        [
            _breakdown("2026-06-01", total=150.0, usage=100.0),
            _breakdown("2026-06-02", total=151.0, usage=100.0),
            _breakdown("2026-06-03", total=149.0, usage=100.0),
            _breakdown("2026-07-01", total=158.2, usage=100.0),
        ],
    )

    assert len(periods) == 2
    assert periods[0].effective_from == date(2026, 6, 1)
    assert periods[0].effective_to == date(2026, 7, 1)
    assert periods[0].standing_source == "inferred_residual_median"
    assert periods[1].standing_source == "tariff_list"


def test_residual_inference_ignores_incomplete_missing_usage_and_nonpositive_values() -> None:
    periods = derive_tariff_periods(
        [{"effectiveDate": "2026-07-01", "plan": []}],
        [
            _breakdown("2026-07-01", total=100.0, usage=None),
            _breakdown("2026-07-02", total=100.0, usage=40.0, complete=False),
            _breakdown("2026-07-03", total=40.0, usage=40.0),
            _breakdown("2026-07-04", total=90.0, usage=40.0),
        ],
    )

    assert periods[0].sample_days == 1
    assert periods[0].standing_pence is None


def test_current_configured_standing_falls_back_when_no_residual_cluster() -> None:
    period = derive_tariff_periods(
        [{"effectiveDate": "2026-07-01", "plan": []}],
        [_breakdown("2026-07-01", total=100.0, usage=None)],
        configured_standing_gbp=0.582,
        configured_rate_gbp_per_kwh=0.245,
    )[0]

    assert period.standing_pence == 58.2
    assert period.standing_source == "configured_current_fallback"
    assert period.unit_rate_pence_per_kwh == 24.5
    assert period.unit_rate_source == "configured_current_fallback"


def test_current_config_mismatch_keeps_inferred_standing_and_flags_source() -> None:
    period = derive_tariff_periods(
        [{"effectiveDate": "2026-07-01", "plan": []}],
        [
            _breakdown("2026-07-01", total=150.0, usage=100.0),
            _breakdown("2026-07-02", total=150.0, usage=100.0),
            _breakdown("2026-07-03", total=150.0, usage=100.0),
        ],
        configured_standing_gbp=0.80,
    )[0]

    assert period.standing_pence == 50.0
    assert period.standing_source == "inferred_current_config_mismatch"
    assert period.configured_standing_delta_pence == -30.0


def test_tariff_period_serialisation_uses_iso_dates() -> None:
    period = parse_tariff_periods(
        [
            {"effectiveDate": "2026-07-01", "plan": [{"rate": 24.5}]},
            {"effectiveDate": "2026-08-01", "plan": [{"rate": 25.0}]},
        ]
    )[0]

    value = tariff_period_as_dict(period)
    assert value["effective_from"] == "2026-07-01"
    assert value["effective_to"] == "2026-08-01"
