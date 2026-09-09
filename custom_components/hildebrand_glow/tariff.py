"""Effective-dated tariff interpretation and cost-component normalisation."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from statistics import median
from typing import Any

from .api import UK_TZ
from .costing import CostBreakdown

MIN_INFERENCE_SAMPLES = 3
CURRENT_CALIBRATION_MIN_TOLERANCE_PENCE = 5.0
PENCE_PRECISION = 6


@dataclass(frozen=True)
class TariffPeriod:
    """One effective tariff period and the evidence used to resolve it."""

    effective_from: date
    effective_to: date | None
    standing_pence: float | None
    standing_source: str
    unit_rate_pence_per_kwh: float | None
    unit_rate_source: str
    rate_kind: str
    sample_days: int = 0
    residual_median_pence: float | None = None
    residual_mad_pence: float | None = None
    configured_standing_delta_pence: float | None = None
    configured_unit_rate_delta_pence: float | None = None


def _round_pence(value: float) -> float:
    """Normalise tariff arithmetic without discarding sub-penny precision."""
    return round(float(value), PENCE_PRECISION)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _effective_date(item: dict[str, Any]) -> date | None:
    raw = item.get("effectiveDate") or item.get("from") or item.get("effective")
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    if value.tzinfo is not None:
        value = value.astimezone(UK_TZ)
    return value.date()


def _walk_plan(value: Any, found: dict[str, list[Any]]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in found:
                found[key].append(child)
            _walk_plan(child, found)
    elif isinstance(value, list):
        for child in value:
            _walk_plan(child, found)


def _unique_numbers(values: list[Any]) -> list[float]:
    result: list[float] = []
    for value in values:
        parsed = _number(value)
        if parsed is None:
            continue
        parsed = _round_pence(parsed)
        if not any(abs(parsed - existing) < 1e-9 for existing in result):
            result.append(parsed)
    return result


def _tariff_values(item: dict[str, Any]) -> tuple[float | None, float | None, str]:
    found: dict[str, list[Any]] = {
        "standing": [],
        "standingCharge": [],
        "rate": [],
        "tourate": [],
        "dynamic": [],
        "tier": [],
        "time": [],
    }
    _walk_plan(item.get("plan", item), found)

    standing_values = _unique_numbers(found["standing"] + found["standingCharge"])
    standing = _round_pence(median(standing_values)) if standing_values else None

    rate_values = _unique_numbers(found["rate"] + found["tourate"])
    has_dynamic = any(value not in (None, "", False) for value in found["dynamic"])
    has_time_windows = any(value not in (None, "", False) for value in found["time"])
    has_tou_rates = any(value not in (None, "", False) for value in found["tourate"])
    has_tiers = any(value not in (None, "", False) for value in found["tier"])

    if has_dynamic:
        return standing, None, "dynamic"

    # Glow's documented flat-rate shape may put ``tier: 1`` alongside the sole
    # unit rate. A tier marker by itself therefore does not make a tariff TOU.
    # Time windows / explicit TOU rates are the discriminators for TOU, while
    # multiple tiered rates without time windows represent a block tariff.
    if has_time_windows or has_tou_rates:
        return standing, None, "tou"
    if len(rate_values) == 1:
        return standing, rate_values[0], "flat"
    if len(rate_values) > 1 and has_tiers:
        return standing, None, "block"
    return standing, None, "unknown"


def parse_tariff_periods(rows: list[dict[str, Any]]) -> list[TariffPeriod]:
    """Parse Glow tariff-list rows into sorted effective periods."""
    candidates: list[tuple[date, float | None, float | None, str]] = []
    for row in rows:
        effective = _effective_date(row)
        if effective is None:
            continue
        standing, unit_rate, rate_kind = _tariff_values(row)
        candidates.append((effective, standing, unit_rate, rate_kind))

    # Multiple sources can produce a row at the same effective date. Prefer the
    # row carrying the most useful explicit tariff detail.
    best_by_date: dict[date, tuple[date, float | None, float | None, str]] = {}
    for candidate in candidates:
        effective, standing, unit_rate, rate_kind = candidate
        score = int(standing is not None) + int(unit_rate is not None) + int(
            rate_kind != "unknown"
        )
        previous = best_by_date.get(effective)
        if previous is None:
            best_by_date[effective] = candidate
            continue
        previous_score = int(previous[1] is not None) + int(previous[2] is not None) + int(
            previous[3] != "unknown"
        )
        if score >= previous_score:
            best_by_date[effective] = candidate

    ordered = [best_by_date[key] for key in sorted(best_by_date)]
    result: list[TariffPeriod] = []
    for index, (effective, standing, unit_rate, rate_kind) in enumerate(ordered):
        effective_to = ordered[index + 1][0] if index + 1 < len(ordered) else None
        result.append(
            TariffPeriod(
                effective_from=effective,
                effective_to=effective_to,
                standing_pence=standing,
                standing_source="tariff_list" if standing is not None else "unavailable",
                unit_rate_pence_per_kwh=unit_rate,
                unit_rate_source="tariff_list" if unit_rate is not None else "unavailable",
                rate_kind=rate_kind,
            )
        )
    return result


def _contains(period: TariffPeriod, day: date) -> bool:
    return day >= period.effective_from and (
        period.effective_to is None or day < period.effective_to
    )


def _period_residuals(
    history: list[CostBreakdown], period: TariffPeriod
) -> list[float]:
    residuals: list[float] = []
    for breakdown in history:
        if breakdown.usage_pence is None or not breakdown.complete_day:
            continue
        day = date.fromisoformat(breakdown.day)
        if not _contains(period, day):
            continue
        residual = _round_pence(
            float(breakdown.total_pence) - float(breakdown.usage_pence)
        )
        if residual > 0:
            residuals.append(residual)
    return residuals


def derive_tariff_periods(
    rows: list[dict[str, Any]],
    history: list[CostBreakdown],
    *,
    configured_standing_gbp: float | None = None,
    configured_rate_gbp_per_kwh: float | None = None,
) -> list[TariffPeriod]:
    """Resolve standing charges per effective tariff period.

    Glow tariff-list plan data is preferred. Where a historical tariff row does
    not expose a standing charge, the median P1D-minus-PT30M residual for that
    exact period is used. The configured current tariff is a calibration anchor:
    if it agrees with the current residual cluster it supplies the exact current
    value rather than preserving aggregation noise.
    """
    periods = parse_tariff_periods(rows)
    if not history:
        return periods

    first_history_day = date.fromisoformat(history[0].day)
    if not periods:
        periods = [
            TariffPeriod(
                effective_from=first_history_day,
                effective_to=None,
                standing_pence=None,
                standing_source="unavailable",
                unit_rate_pence_per_kwh=None,
                unit_rate_source="unavailable",
                rate_kind="unknown",
            )
        ]
    elif first_history_day < periods[0].effective_from:
        periods.insert(
            0,
            TariffPeriod(
                effective_from=first_history_day,
                effective_to=periods[0].effective_from,
                standing_pence=None,
                standing_source="unavailable",
                unit_rate_pence_per_kwh=None,
                unit_rate_source="unavailable",
                rate_kind="unknown",
            ),
        )

    configured_standing_pence = (
        _round_pence(float(configured_standing_gbp) * 100.0)
        if configured_standing_gbp is not None
        else None
    )
    configured_rate_pence = (
        _round_pence(float(configured_rate_gbp_per_kwh) * 100.0)
        if configured_rate_gbp_per_kwh is not None
        else None
    )

    resolved: list[TariffPeriod] = []
    for index, period in enumerate(periods):
        residuals = _period_residuals(history, period)
        residual_median = (
            _round_pence(median(residuals)) if residuals else None
        )
        residual_mad = (
            _round_pence(
                median([abs(value - residual_median) for value in residuals])
            )
            if residual_median is not None
            else None
        )

        standing = (
            _round_pence(period.standing_pence)
            if period.standing_pence is not None
            else None
        )
        standing_source = period.standing_source
        if standing is None and len(residuals) >= MIN_INFERENCE_SAMPLES:
            standing = residual_median
            standing_source = "inferred_residual_median"

        unit_rate = (
            _round_pence(period.unit_rate_pence_per_kwh)
            if period.unit_rate_pence_per_kwh is not None
            else None
        )
        unit_rate_source = period.unit_rate_source
        is_current = index == len(periods) - 1 and period.effective_to is None

        standing_delta = None
        rate_delta = None
        if is_current and configured_standing_pence is not None:
            if standing is not None:
                standing_delta = _round_pence(standing - configured_standing_pence)
            if period.standing_pence is None:
                if standing is None:
                    standing = configured_standing_pence
                    standing_source = "configured_current_fallback"
                else:
                    tolerance = max(
                        CURRENT_CALIBRATION_MIN_TOLERANCE_PENCE,
                        3.0 * float(residual_mad or 0.0),
                    )
                    if abs(standing_delta or 0.0) <= tolerance:
                        standing = configured_standing_pence
                        standing_source = "configured_current_calibrated"
                        standing_delta = 0.0
                    else:
                        standing_source = "inferred_current_config_mismatch"

        if is_current and configured_rate_pence is not None:
            if unit_rate is not None:
                rate_delta = _round_pence(unit_rate - configured_rate_pence)
            elif period.rate_kind == "unknown":
                unit_rate = configured_rate_pence
                unit_rate_source = "configured_current_fallback"
                rate_delta = 0.0

        resolved.append(
            replace(
                period,
                standing_pence=standing,
                standing_source=standing_source,
                unit_rate_pence_per_kwh=unit_rate,
                unit_rate_source=unit_rate_source,
                sample_days=len(residuals),
                residual_median_pence=residual_median,
                residual_mad_pence=residual_mad,
                configured_standing_delta_pence=standing_delta,
                configured_unit_rate_delta_pence=rate_delta,
            )
        )
    return resolved


def tariff_period_as_dict(period: TariffPeriod) -> dict[str, Any]:
    """Return JSON-safe tariff-period diagnostics for Home Assistant Store."""
    value = asdict(period)
    value["effective_from"] = period.effective_from.isoformat()
    value["effective_to"] = (
        period.effective_to.isoformat() if period.effective_to is not None else None
    )
    return value
