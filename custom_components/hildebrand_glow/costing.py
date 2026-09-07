"""Glow cost aggregation helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .api import HISTORY_INTERVAL_DAYS, UK_TZ, GlowmarktApiClient

COST_DAILY_INTERVAL_DAYS = 30


@dataclass(frozen=True)
class CostBreakdown:
    """Authoritative Glow cost plus its observed standing-charge component."""

    day: str
    total_pence: float
    usage_pence: float | None
    standing_charge_pence: float | None
    standing_charge_status: str
    complete_day: bool
    usage_intervals: tuple[tuple[datetime, float], ...] = ()


def _window_intervals(
    rows: list[list[Any]],
    start_uk: datetime,
    end_uk: datetime,
) -> list[tuple[datetime, float]]:
    """Return non-null rows whose timestamps fall inside one UK-local window."""
    values: list[tuple[datetime, float]] = []
    for row in rows:
        if len(row) <= 1 or row[1] is None:
            continue
        timestamp = datetime.fromtimestamp(float(row[0]), tz=timezone.utc)
        timestamp_uk = timestamp.astimezone(UK_TZ)
        if start_uk <= timestamp_uk < end_uk:
            values.append((timestamp, float(row[1])))
    values.sort(key=lambda item: item[0])
    return values


def _window_total(
    rows: list[list[Any]],
    start_uk: datetime,
    end_uk: datetime,
) -> float | None:
    intervals = _window_intervals(rows, start_uk, end_uk)
    if not intervals:
        return None
    return round(sum(value for _, value in intervals), 3)


def _completed_breakdown(
    *,
    day: str,
    pt30m_pence: float | None,
    p1d_pence: float | None,
    usage_intervals: tuple[tuple[datetime, float], ...] = (),
    tolerance_pence: float = 1.0,
) -> CostBreakdown | None:
    """Reconcile Glow PT30M usage cost against its completed P1D total."""
    if pt30m_pence is None and p1d_pence is None:
        return None

    if p1d_pence is None:
        return CostBreakdown(
            day=day,
            total_pence=float(pt30m_pence),
            usage_pence=float(pt30m_pence),
            standing_charge_pence=None,
            standing_charge_status="daily_pending",
            complete_day=True,
            usage_intervals=usage_intervals,
        )

    if pt30m_pence is None:
        return CostBreakdown(
            day=day,
            total_pence=float(p1d_pence),
            usage_pence=None,
            standing_charge_pence=None,
            standing_charge_status="unknown",
            complete_day=True,
            usage_intervals=usage_intervals,
        )

    residual = round(float(p1d_pence) - float(pt30m_pence), 3)
    if abs(residual) <= tolerance_pence:
        standing = 0.0
        status = "not_applied"
    elif residual > tolerance_pence:
        standing = residual
        status = "applied"
    else:
        standing = None
        status = "unknown"

    return CostBreakdown(
        day=day,
        total_pence=float(p1d_pence),
        usage_pence=float(pt30m_pence),
        standing_charge_pence=standing,
        standing_charge_status=status,
        complete_day=True,
        usage_intervals=usage_intervals,
    )


def _group_pt30m_by_day(
    rows: list[list[Any]],
    start_uk: datetime,
    end_uk: datetime,
) -> dict[str, list[tuple[datetime, float]]]:
    grouped: dict[str, list[tuple[datetime, float]]] = {}
    for timestamp, value in _window_intervals(rows, start_uk, end_uk):
        day = timestamp.astimezone(UK_TZ).date().isoformat()
        grouped.setdefault(day, []).append((timestamp, value))
    return grouped


def _group_daily_by_day(
    rows: list[list[Any]],
    start_uk: datetime,
    end_uk: datetime,
) -> dict[str, float]:
    grouped: dict[str, float] = {}
    for timestamp, value in _window_intervals(rows, start_uk, end_uk):
        day = timestamp.astimezone(UK_TZ).date().isoformat()
        grouped[day] = round(float(value), 3)
    return grouped


async def get_latest_cost_breakdown(
    api_client: GlowmarktApiClient,
    resource_id: str,
    *,
    now_uk: datetime | None = None,
    completed_lookback_days: int = 3,
) -> CostBreakdown | None:
    """Return the latest Glow cost using aggregation semantics as the authority.

    Hildebrand cost resources omit standing charge from PT30M/hourly data and
    include it in daily/weekly/monthly aggregation. Current-day cost therefore uses
    PT30M only and reports zero standing charge. For completed days, P1D is the
    authoritative total and P1D minus PT30M is the observed standing charge.
    """
    if now_uk is None:
        now_uk = datetime.now(UK_TZ)
    else:
        now_uk = now_uk.astimezone(UK_TZ)

    today_start = now_uk.replace(hour=0, minute=0, second=0, microsecond=0)

    if now_uk > today_start:
        current_rows = await api_client._request_readings(  # noqa: SLF001
            resource_id,
            today_start,
            now_uk,
            "PT30M",
        )
        current_intervals = _window_intervals(current_rows, today_start, now_uk)
        if current_intervals:
            current_total = round(sum(value for _, value in current_intervals), 3)
            return CostBreakdown(
                day=today_start.date().isoformat(),
                total_pence=current_total,
                usage_pence=current_total,
                standing_charge_pence=0.0,
                standing_charge_status="not_applied",
                complete_day=False,
                usage_intervals=tuple(current_intervals),
            )

    for days_back in range(1, completed_lookback_days + 1):
        day_start = today_start - timedelta(days=days_back)
        day_end = today_start - timedelta(days=days_back - 1)
        pt30m_rows = await api_client._request_readings(  # noqa: SLF001
            resource_id,
            day_start,
            day_end,
            "PT30M",
        )
        p1d_rows = await api_client._request_readings(  # noqa: SLF001
            resource_id,
            day_start,
            day_end,
            "P1D",
        )
        usage_intervals = tuple(_window_intervals(pt30m_rows, day_start, day_end))
        breakdown = _completed_breakdown(
            day=day_start.date().isoformat(),
            pt30m_pence=(
                round(sum(value for _, value in usage_intervals), 3)
                if usage_intervals
                else None
            ),
            p1d_pence=_window_total(p1d_rows, day_start, day_end),
            usage_intervals=usage_intervals,
        )
        if breakdown is not None:
            return breakdown

    return None


async def get_cost_history(
    api_client: GlowmarktApiClient,
    resource_id: str,
    *,
    now_uk: datetime | None = None,
) -> list[CostBreakdown]:
    """Fetch all complete historical usage and standing-charge components.

    PT30M cost data supplies the usage-only shape. P1D supplies the authoritative
    completed-day total. Their residual is the observed standing charge for that
    day. The current UK-local day is deliberately excluded.
    """
    if now_uk is None:
        now_uk = datetime.now(UK_TZ)
    else:
        now_uk = now_uk.astimezone(UK_TZ)

    first = await api_client.get_first_available_reading_time(resource_id)
    if first is None:
        return []
    start_uk = first.astimezone(UK_TZ).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    today_start = now_uk.replace(hour=0, minute=0, second=0, microsecond=0)
    if start_uk >= today_start:
        return []

    usage_by_day: dict[str, list[tuple[datetime, float]]] = {}
    chunk_start = start_uk
    while chunk_start < today_start:
        chunk_end = min(
            chunk_start + timedelta(days=HISTORY_INTERVAL_DAYS),
            today_start,
        )
        rows = await api_client._request_readings(  # noqa: SLF001
            resource_id,
            chunk_start,
            chunk_end,
            "PT30M",
        )
        for day, intervals in _group_pt30m_by_day(
            rows,
            chunk_start,
            chunk_end,
        ).items():
            usage_by_day.setdefault(day, []).extend(intervals)
        chunk_start = chunk_end

    daily_by_day: dict[str, float] = {}
    chunk_start = start_uk
    while chunk_start < today_start:
        chunk_end = min(
            chunk_start + timedelta(days=COST_DAILY_INTERVAL_DAYS),
            today_start,
        )
        rows = await api_client._request_readings(  # noqa: SLF001
            resource_id,
            chunk_start,
            chunk_end,
            "P1D",
        )
        daily_by_day.update(_group_daily_by_day(rows, chunk_start, chunk_end))
        chunk_start = chunk_end

    history: list[CostBreakdown] = []
    all_days = sorted(set(usage_by_day) | set(daily_by_day))
    for day in all_days:
        intervals = tuple(sorted(usage_by_day.get(day, []), key=lambda item: item[0]))
        usage = round(sum(value for _, value in intervals), 3) if intervals else None
        breakdown = _completed_breakdown(
            day=day,
            pt30m_pence=usage,
            p1d_pence=daily_by_day.get(day),
            usage_intervals=intervals,
        )
        if breakdown is not None:
            history.append(breakdown)
    return history
