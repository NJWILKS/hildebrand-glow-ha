"""Tariff-first pricing of Hildebrand consumption and cost history."""
from __future__ import annotations

from datetime import date
from typing import Any

from .api import DailyReading
from .costing import CostBreakdown
from .tariff import TariffPeriod

PENCE_PRECISION = 6


def _round_pence(value: float) -> float:
    return round(float(value), PENCE_PRECISION)


def _period_for(day: date, periods: list[TariffPeriod]) -> TariffPeriod | None:
    return next(
        (
            period
            for period in periods
            if day >= period.effective_from
            and (period.effective_to is None or day < period.effective_to)
        ),
        None,
    )


def _consumption_by_day(
    readings: list[DailyReading],
) -> dict[str, DailyReading]:
    return {reading.day: reading for reading in readings}


def price_cost_history(
    history: list[CostBreakdown],
    consumption: list[DailyReading],
    periods: list[TariffPeriod],
) -> tuple[list[CostBreakdown], list[dict[str, Any]]]:
    """Price usage and standing charge from the effective tariff timeline.

    For a flat tariff, PT30M consumption multiplied by the effective unit rate is
    the authoritative usage-cost component. The effective standing charge is then
    applied exactly once for the UK-local billing day.

    For TOU/dynamic tariffs, where a single unit rate cannot describe the day,
    Glow's PT30M cost resource remains the authoritative usage-cost shape while the
    effective tariff standing charge is still applied exactly once.

    Glow P1D cost is retained as a reconciliation oracle. It is not used to distort
    the two chart components when an explicit tariff can price them directly.
    """
    consumption_days = _consumption_by_day(consumption)
    priced: list[CostBreakdown] = []
    diagnostics: list[dict[str, Any]] = []

    for breakdown in history:
        day = date.fromisoformat(breakdown.day)
        period = _period_for(day, periods)
        reading = consumption_days.get(breakdown.day)

        usage_source = "unavailable"
        usage_pence: float | None = None
        usage_intervals: tuple[tuple[Any, float], ...] = ()
        unit_rate: float | None = None

        if (
            period is not None
            and period.rate_kind == "flat"
            and period.unit_rate_pence_per_kwh is not None
            and reading is not None
            and reading.intervals
        ):
            unit_rate = _round_pence(period.unit_rate_pence_per_kwh)
            usage_intervals = tuple(
                (
                    timestamp,
                    _round_pence(float(kwh) * unit_rate),
                )
                for timestamp, kwh in reading.intervals
            )
            usage_pence = _round_pence(
                sum(value for _timestamp, value in usage_intervals)
            )
            usage_source = "tariff_flat_x_consumption"
        elif breakdown.usage_intervals:
            usage_intervals = tuple(
                (timestamp, _round_pence(value))
                for timestamp, value in breakdown.usage_intervals
            )
            usage_pence = _round_pence(
                sum(value for _timestamp, value in usage_intervals)
            )
            usage_source = "glow_pt30m_cost"
        elif breakdown.usage_pence is not None:
            usage_pence = _round_pence(breakdown.usage_pence)
            usage_source = "glow_usage_total"

        standing_pence = (
            _round_pence(period.standing_pence)
            if period is not None and period.standing_pence is not None
            else (
                _round_pence(breakdown.standing_charge_pence)
                if breakdown.standing_charge_pence is not None
                else None
            )
        )
        standing_source = (
            period.standing_source
            if period is not None and period.standing_pence is not None
            else breakdown.standing_charge_status
        )

        if usage_pence is not None and standing_pence is not None:
            total_pence = _round_pence(usage_pence + standing_pence)
        else:
            total_pence = _round_pence(breakdown.total_pence)

        glow_total = _round_pence(breakdown.total_pence)
        reconciliation_delta = _round_pence(total_pence - glow_total)

        priced.append(
            CostBreakdown(
                day=breakdown.day,
                total_pence=total_pence,
                usage_pence=usage_pence,
                standing_charge_pence=standing_pence,
                standing_charge_status=standing_source,
                complete_day=breakdown.complete_day,
                usage_intervals=usage_intervals,
            )
        )
        diagnostics.append(
            {
                "day": breakdown.day,
                "rate_kind": period.rate_kind if period is not None else "unknown",
                "unit_rate_pence_per_kwh": unit_rate,
                "unit_rate_source": (
                    period.unit_rate_source if period is not None else "unavailable"
                ),
                "usage_source": usage_source,
                "usage_pence": usage_pence,
                "standing_pence": standing_pence,
                "standing_source": standing_source,
                "calculated_total_pence": total_pence,
                "glow_p1d_total_pence": glow_total,
                "glow_reconciliation_delta_pence": reconciliation_delta,
            }
        )

    return priced, diagnostics
