"""Deterministic projections from the PT30M interval ledger.

This module deliberately does not write to Recorder.  It turns immutable raw
interval facts plus effective-dated tariff metadata into the daily component
rows that a later Home Assistant statistics writer can publish.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from homeassistant.components.recorder.models import StatisticData

from .api import UK_TZ

STAT_PRECISION = 6


@dataclass(frozen=True)
class DailyCostProjection:
    """One UK-local billing day's chartable cost components in pence."""

    day: date
    usage_pence: float | None
    standing_pence: float | None
    total_pence: float | None
    standing_source: str | None


def _round_stat(value: float) -> float:
    return round(float(value), STAT_PRECISION)


def _gbp(value_pence: float) -> float:
    return _round_stat(float(value_pence) / 100.0)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _period_for_day(
    periods: list[dict[str, Any]],
    day: date,
) -> dict[str, Any] | None:
    """Return the effective tariff period for a UK-local billing day."""
    for period in periods:
        raw_from = period.get("effective_from")
        if not raw_from:
            continue
        try:
            effective_from = date.fromisoformat(str(raw_from)[:10])
        except ValueError:
            continue
        raw_to = period.get("effective_to")
        try:
            effective_to = date.fromisoformat(str(raw_to)[:10]) if raw_to else None
        except ValueError:
            effective_to = None
        if day >= effective_from and (effective_to is None or day < effective_to):
            return period
    return None


def build_daily_cost_projection(
    intervals: list[dict[str, Any]],
    periods: list[dict[str, Any]],
) -> list[DailyCostProjection]:
    """Aggregate raw PT30M cost into stackable UK-local daily components.

    Glow PT30M cost is treated as usage cost.  The standing charge comes only
    from the tariff period effective for that billing day and is applied exactly
    once.  If either component is unknown the total is left unknown rather than
    inventing a deceptively low daily total.
    """
    by_day: dict[date, dict[str, Any]] = {}
    for interval in intervals:
        timestamp_raw = interval.get("timestamp")
        if not timestamp_raw:
            continue
        try:
            timestamp = _parse_utc(str(timestamp_raw))
        except ValueError:
            continue
        day = timestamp.astimezone(UK_TZ).date()
        entry = by_day.setdefault(day, {"usage_pence": 0.0, "cost_seen": False})
        cost = interval.get("cost_pence")
        if cost is not None:
            entry["usage_pence"] = float(entry["usage_pence"]) + float(cost)
            entry["cost_seen"] = True

    result: list[DailyCostProjection] = []
    for day in sorted(by_day):
        values = by_day[day]
        usage_pence = (
            _round_stat(float(values["usage_pence"]))
            if values["cost_seen"]
            else None
        )
        period = _period_for_day(periods, day)
        standing_raw = period.get("standing_pence") if period is not None else None
        standing_pence = (
            _round_stat(float(standing_raw)) if standing_raw is not None else None
        )
        total_pence = (
            _round_stat(usage_pence + standing_pence)
            if usage_pence is not None and standing_pence is not None
            else None
        )
        result.append(
            DailyCostProjection(
                day=day,
                usage_pence=usage_pence,
                standing_pence=standing_pence,
                total_pence=total_pence,
                standing_source=(
                    str(period.get("standing_source"))
                    if period is not None and period.get("standing_source") is not None
                    else None
                ),
            )
        )
    return result


def build_cost_stack_statistics(
    daily: list[DailyCostProjection],
    *,
    usage_baseline: float = 0.0,
    standing_baseline: float = 0.0,
    total_baseline: float = 0.0,
) -> tuple[list[StatisticData], list[StatisticData], list[StatisticData]]:
    """Build monotonic statistics for usage, standing and total daily cost.

    Each ``state`` is the whole UK-local day's component in GBP.  Each ``sum``
    is monotonic, making Home Assistant's daily ``change`` exactly equal to the
    bar height.  Usage and standing can therefore be stacked without plotting
    total as a third series and visually double-counting it.
    """
    usage_running = _round_stat(usage_baseline)
    standing_running = _round_stat(standing_baseline)
    total_running = _round_stat(total_baseline)
    usage_stats: list[StatisticData] = []
    standing_stats: list[StatisticData] = []
    total_stats: list[StatisticData] = []

    for projection in sorted(daily, key=lambda item: item.day):
        start = datetime.combine(projection.day, datetime.min.time(), tzinfo=UK_TZ).astimezone(
            timezone.utc
        )

        if projection.usage_pence is not None:
            state = _gbp(projection.usage_pence)
            usage_running = _round_stat(usage_running + state)
            usage_stats.append(StatisticData(start=start, state=state, sum=usage_running))

        if projection.standing_pence is not None:
            state = _gbp(projection.standing_pence)
            standing_running = _round_stat(standing_running + state)
            standing_stats.append(
                StatisticData(start=start, state=state, sum=standing_running)
            )

        if projection.total_pence is not None:
            state = _gbp(projection.total_pence)
            total_running = _round_stat(total_running + state)
            total_stats.append(StatisticData(start=start, state=state, sum=total_running))

    return usage_stats, standing_stats, total_stats
