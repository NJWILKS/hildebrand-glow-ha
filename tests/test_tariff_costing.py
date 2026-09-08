from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from custom_components.hildebrand_glow.api import DailyReading
from custom_components.hildebrand_glow.cost_ingestion import build_component_statistics
from custom_components.hildebrand_glow.costing import CostBreakdown
from custom_components.hildebrand_glow.tariff import TariffPeriod
from custom_components.hildebrand_glow.tariff_costing import price_cost_history


def _period(
    *,
    start: date = date(2026, 7, 1),
    end: date | None = None,
    standing: float | None = 58.2,
    rate: float | None = 24.5,
    kind: str = "flat",
) -> TariffPeriod:
    return TariffPeriod(
        effective_from=start,
        effective_to=end,
        standing_pence=standing,
        standing_source="tariff_list" if standing is not None else "unavailable",
        unit_rate_pence_per_kwh=rate,
        unit_rate_source="tariff_list" if rate is not None else "unavailable",
        rate_kind=kind,
    )


def _consumption(day: str, values: list[float]) -> DailyReading:
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    intervals = [
        (start + timedelta(minutes=30 * index), value)
        for index, value in enumerate(values)
    ]
    return DailyReading(day=day, value=sum(values), intervals=intervals)


def _glow_cost(
    day: str,
    *,
    usage: float,
    total: float,
    complete: bool = True,
) -> CostBreakdown:
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return CostBreakdown(
        day=day,
        total_pence=total,
        usage_pence=usage,
        standing_charge_pence=(total - usage if complete else 0.0),
        standing_charge_status="applied" if complete else "not_applied",
        complete_day=complete,
        usage_intervals=((start, usage),),
    )


def test_flat_tariff_prices_usage_from_consumption_and_adds_standing_once() -> None:
    history = [_glow_cost("2026-09-07", usage=72.0, total=130.0)]
    consumption = [_consumption("2026-09-07", [1.0, 2.0])]

    priced, diagnostics = price_cost_history(history, consumption, [_period()])

    assert len(priced) == 1
    day = priced[0]
    assert day.usage_pence == 73.5
    assert day.standing_charge_pence == 58.2
    assert day.total_pence == 131.7
    assert [value for _timestamp, value in day.usage_intervals] == [24.5, 49.0]

    diagnostic = diagnostics[0]
    assert diagnostic["unit_rate_pence_per_kwh"] == 24.5
    assert diagnostic["usage_source"] == "tariff_flat_x_consumption"
    assert diagnostic["standing_source"] == "tariff_list"
    assert diagnostic["glow_reconciliation_delta_pence"] == 1.7


def test_effective_tariff_change_prices_each_day_with_its_own_rate_and_standing() -> None:
    periods = [
        _period(
            start=date(2026, 4, 1),
            end=date(2026, 7, 1),
            standing=55.2,
            rate=23.1,
        ),
        _period(start=date(2026, 7, 1), standing=58.2, rate=24.5),
    ]
    history = [
        _glow_cost("2026-06-30", usage=23.0, total=78.0),
        _glow_cost("2026-07-01", usage=24.0, total=82.0),
    ]
    consumption = [
        _consumption("2026-06-30", [1.0]),
        _consumption("2026-07-01", [1.0]),
    ]

    priced, _diagnostics = price_cost_history(history, consumption, periods)

    assert [(item.usage_pence, item.standing_charge_pence) for item in priced] == [
        (23.1, 55.2),
        (24.5, 58.2),
    ]
    assert [item.total_pence for item in priced] == [78.3, 82.7]


def test_tou_tariff_keeps_glow_pt30m_usage_but_uses_tariff_standing() -> None:
    history = [_glow_cost("2026-09-07", usage=123.4, total=180.0)]
    period = _period(standing=58.2, rate=None, kind="tou")

    priced, diagnostics = price_cost_history(history, [], [period])

    assert priced[0].usage_pence == 123.4
    assert priced[0].standing_charge_pence == 58.2
    assert priced[0].total_pence == 181.6
    assert diagnostics[0]["usage_source"] == "glow_pt30m_cost"
    assert diagnostics[0]["rate_kind"] == "tou"


def test_open_day_stack_includes_known_standing_charge_immediately() -> None:
    history = [
        _glow_cost(
            "2026-09-08",
            usage=49.0,
            total=49.0,
            complete=False,
        )
    ]
    consumption = [_consumption("2026-09-08", [1.0, 1.0])]

    priced, _diagnostics = price_cost_history(history, consumption, [_period()])
    usage_stats, standing_stats = build_component_statistics(priced)

    assert priced[0].usage_pence == 49.0
    assert priced[0].standing_charge_pence == 58.2
    assert priced[0].total_pence == 107.2
    assert usage_stats[-1]["state"] == 0.49
    assert standing_stats[-1]["state"] == 0.582
    assert standing_stats[-1]["sum"] == 0.582
