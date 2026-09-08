"""Integration-owned Home Assistant Energy consumption statistics."""
from __future__ import annotations

import re
from datetime import datetime

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import EnergyConverter

from .api import DailyReading
from .const import DOMAIN

STAT_PRECISION = 6


def _round_stat(value: float) -> float:
    return round(float(value), STAT_PRECISION)


def energy_consumption_statistic_id(site_id: str, commodity: str) -> str:
    """Return the stable external Energy consumption statistic ID."""
    safe_site = re.sub(r"[^a-z0-9_]+", "_", site_id.lower()).strip("_")
    safe_commodity = re.sub(r"[^a-z0-9_]+", "_", commodity.lower()).strip("_")
    return f"{DOMAIN}:{safe_site}_{safe_commodity}_energy_consumption"


def consumption_metadata(site_id: str, commodity: str) -> StatisticMetaData:
    """Return Recorder metadata for an integration-owned consumption series."""
    return StatisticMetaData(
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name=f"Hildebrand Glow {commodity.title()} Energy Consumption",
        source=DOMAIN,
        statistic_id=energy_consumption_statistic_id(site_id, commodity),
        unit_class=EnergyConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )


def build_consumption_statistics(
    readings: list[DailyReading],
    *,
    baseline: float = 0.0,
) -> list[StatisticData]:
    """Build hourly interval states with one monotonic cumulative Energy sum.

    Glow PT30M values are interval energy, not a lifetime register. ``state`` is
    therefore the energy used in that UTC hour, while ``sum`` is the running
    cumulative total Home Assistant's Energy dashboard consumes.

    Re-importing the same hour is idempotent because external statistics replace
    rows by timestamp. Negative source intervals are rejected rather than allowing
    a malformed/revised payload to corrupt the Energy series.
    """
    running = _round_stat(baseline)
    stats: list[StatisticData] = []

    for reading in sorted(readings, key=lambda item: item.day):
        hourly: dict[datetime, float] = {}
        for timestamp, raw_value in reading.intervals:
            value = float(raw_value)
            if value < 0:
                raise ValueError(
                    f"Negative Glow consumption interval for {reading.day}: {value}"
                )
            hour_start = timestamp.replace(minute=0, second=0, microsecond=0)
            hourly[hour_start] = hourly.get(hour_start, 0.0) + value

        for hour_start in sorted(hourly):
            state = _round_stat(hourly[hour_start])
            running = _round_stat(running + state)
            stats.append(
                StatisticData(
                    start=hour_start,
                    state=state,
                    sum=running,
                )
            )

    return stats


def add_consumption_statistics(
    hass: HomeAssistant,
    site_id: str,
    commodity: str,
    readings: list[DailyReading],
    *,
    baseline: float = 0.0,
) -> tuple[list[StatisticData], float]:
    """Add integration-owned Energy statistics and return their ending sum."""
    stats = build_consumption_statistics(readings, baseline=baseline)
    if stats:
        async_add_external_statistics(
            hass,
            consumption_metadata(site_id, commodity),
            stats,
        )
        return stats, _round_stat(float(stats[-1]["sum"]))
    return stats, _round_stat(baseline)
