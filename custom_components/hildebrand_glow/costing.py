"""Glow cost aggregation helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .api import UK_TZ, GlowmarktApiClient


@dataclass(frozen=True)
class CostBreakdown:
    """Authoritative Glow cost plus its observed standing-charge component."""

    day: str
    total_pence: float
    usage_pence: float | None
    standing_charge_pence: float | None
    standing_charge_status: str
    complete_day: bool


def _window_total(
    rows: list[list[Any]],
    start_uk: datetime,
    end_uk: datetime,
) -> float | None:
    """Sum non-null rows whose UTC timestamps fall inside one UK-local window."""
    values: list[float] = []
    for row in rows:
        if len(row) <= 1 or row[1] is None:
            continue
        timestamp = datetime.fromtimestamp(float(row[0]), tz=timezone.utc)
        timestamp_uk = timestamp.astimezone(UK_TZ)
        if start_uk <= timestamp_uk < end_uk:
            values.append(float(row[1]))
    if not values:
        return None
    return round(sum(values), 3)


def _completed_breakdown(
    *,
    day: str,
    pt30m_pence: float | None,
    p1d_pence: float | None,
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
        )

    if pt30m_pence is None:
        return CostBreakdown(
            day=day,
            total_pence=float(p1d_pence),
            usage_pence=None,
            standing_charge_pence=None,
            standing_charge_status="unknown",
            complete_day=True,
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
    )


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
        current_total = _window_total(current_rows, today_start, now_uk)
        if current_total is not None:
            return CostBreakdown(
                day=today_start.date().isoformat(),
                total_pence=current_total,
                usage_pence=current_total,
                standing_charge_pence=0.0,
                standing_charge_status="not_applied",
                complete_day=False,
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
        breakdown = _completed_breakdown(
            day=day_start.date().isoformat(),
            pt30m_pence=_window_total(pt30m_rows, day_start, day_end),
            p1d_pence=_window_total(p1d_rows, day_start, day_end),
        )
        if breakdown is not None:
            return breakdown

    return None
