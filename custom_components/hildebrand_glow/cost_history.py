"""Historical Glow cost/statistics and tariff-ledger maintenance."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .api import GlowmarktApiError, UK_TZ
from .const import (
    CLASSIFIER_ELECTRICITY_COST,
    CLASSIFIER_GAS_COST,
    DOMAIN,
    GLOWMARKT_API_BASE,
)
from .coordinator import GlowmarktDataUpdateCoordinator
from .costing import CostBreakdown, get_cost_history
from .identity import sensor_unique_id

_LOGGER = logging.getLogger(__name__)
COST_HISTORY_STORAGE_VERSION = 1
COST_BACKFILL_SCHEMA_VERSION = 2
TARIFF_HISTORY_STORAGE_VERSION = 1
INITIAL_DELAY_SECONDS = 30
ENTITY_WAIT_RETRIES = 6
ENTITY_WAIT_SECONDS = 5
TARIFF_REFRESH_SECONDS = 24 * 60 * 60

COMPONENT_KEYS = {
    "electricity": (
        CLASSIFIER_ELECTRICITY_COST,
        "electricity_daily_cost",
        "electricity_usage_cost",
        "electricity_standing_charge",
    ),
    "gas": (
        CLASSIFIER_GAS_COST,
        "gas_daily_cost",
        "gas_usage_cost",
        "gas_standing_charge",
    ),
}


def _metadata(entity_id: str) -> StatisticMetaData:
    return StatisticMetaData(
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name=None,
        source="recorder",
        statistic_id=entity_id,
        unit_class=None,
        unit_of_measurement="GBP",
    )


def _day_start_utc(day: str) -> datetime:
    return (
        datetime.fromisoformat(day)
        .replace(tzinfo=UK_TZ)
        .astimezone(timezone.utc)
    )


def build_total_cost_statistics(history: list[CostBreakdown]) -> list[StatisticData]:
    """Build authoritative total-cost statistics for the Energy dashboard.

    Historical PT30M cost preserves the usage-cost shape. Any difference between
    those intervals and the authoritative completed P1D total is applied to the
    first hour of that UK-local day. For normal Bright data that residual is the
    standing charge. If detailed intervals are unavailable, the complete daily
    total is imported at the UK-local day boundary instead.
    """
    running = 0.0
    stats: list[StatisticData] = []

    for breakdown in history:
        total_gbp = round(breakdown.total_pence / 100.0, 2)
        hourly: dict[datetime, float] = {}
        for timestamp, value_pence in breakdown.usage_intervals:
            hour_start = timestamp.replace(minute=0, second=0, microsecond=0)
            hourly[hour_start] = hourly.get(hour_start, 0.0) + value_pence / 100.0

        if not hourly:
            running = round(running + total_gbp, 2)
            stats.append(
                StatisticData(
                    start=_day_start_utc(breakdown.day),
                    state=total_gbp,
                    sum=running,
                )
            )
            continue

        ordered_hours = sorted(hourly)
        usage_gbp = round(sum(hourly.values()), 2)
        residual_gbp = round(total_gbp - usage_gbp, 2)
        if residual_gbp:
            hourly[ordered_hours[0]] = round(
                hourly[ordered_hours[0]] + residual_gbp,
                2,
            )

        for hour_start in ordered_hours:
            state = round(hourly[hour_start], 2)
            running = round(running + state, 2)
            stats.append(
                StatisticData(
                    start=hour_start,
                    state=state,
                    sum=running,
                )
            )

    return stats


def build_component_statistics(
    history: list[CostBreakdown],
) -> tuple[list[StatisticData], list[StatisticData]]:
    """Build daily usage/standing states with cumulative sums for Recorder."""
    usage_running = 0.0
    standing_running = 0.0
    usage_stats: list[StatisticData] = []
    standing_stats: list[StatisticData] = []

    for breakdown in history:
        start = _day_start_utc(breakdown.day)

        if breakdown.usage_pence is not None:
            usage_gbp = round(breakdown.usage_pence / 100.0, 2)
            usage_running = round(usage_running + usage_gbp, 2)
            usage_stats.append(
                StatisticData(
                    start=start,
                    state=usage_gbp,
                    sum=usage_running,
                )
            )

        if breakdown.standing_charge_pence is not None:
            standing_gbp = round(breakdown.standing_charge_pence / 100.0, 2)
            standing_running = round(standing_running + standing_gbp, 2)
            standing_stats.append(
                StatisticData(
                    start=start,
                    state=standing_gbp,
                    sum=standing_running,
                )
            )

    return usage_stats, standing_stats


async def _entity_id(
    hass: HomeAssistant,
    site_id: str,
    sensor_key: str,
) -> str | None:
    registry = er.async_get(hass)
    unique_id = sensor_unique_id(site_id, sensor_key, None)
    for _ in range(ENTITY_WAIT_RETRIES):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is not None:
            return entity_id
        await asyncio.sleep(ENTITY_WAIT_SECONDS)
    return None


async def _backfill_cost_statistics(
    hass: HomeAssistant,
    coordinator: GlowmarktDataUpdateCoordinator,
    site_id: str,
) -> None:
    store = Store(
        hass,
        COST_HISTORY_STORAGE_VERSION,
        f"{DOMAIN}_{site_id}_cost_history",
    )
    state = await store.async_load() or {}
    if state.get("_backfilled_version") == COST_BACKFILL_SCHEMA_VERSION:
        return

    imported: dict[str, int] = {}
    for commodity, (
        cost_classifier,
        daily_key,
        usage_key,
        standing_key,
    ) in COMPONENT_KEYS.items():
        resource = coordinator.resources.get(cost_classifier)
        if not resource:
            continue

        daily_entity = await _entity_id(hass, site_id, daily_key)
        usage_entity = await _entity_id(hass, site_id, usage_key)
        standing_entity = await _entity_id(hass, site_id, standing_key)
        if daily_entity is None or usage_entity is None or standing_entity is None:
            _LOGGER.warning(
                "Cost entities not registered for %s; cost history will retry later",
                commodity,
            )
            return

        history = await get_cost_history(
            coordinator.api_client,
            resource["resource_id"],
        )
        total_stats = build_total_cost_statistics(history)
        usage_stats, standing_stats = build_component_statistics(history)
        if total_stats:
            async_import_statistics(hass, _metadata(daily_entity), total_stats)
        if usage_stats:
            async_import_statistics(hass, _metadata(usage_entity), usage_stats)
        if standing_stats:
            async_import_statistics(hass, _metadata(standing_entity), standing_stats)
        imported[commodity] = len(history)

    state["_backfilled"] = True
    state["_backfilled_version"] = COST_BACKFILL_SCHEMA_VERSION
    state["days"] = imported
    await store.async_save(state)
    if imported:
        _LOGGER.info(
            "Backfilled Glowmarkt Energy cost totals/components: %s",
            ", ".join(f"{commodity}={days} day(s)" for commodity, days in imported.items()),
        )


def _effective_key(item: dict[str, Any]) -> str:
    return str(
        item.get("effectiveDate")
        or item.get("from")
        or item.get("effective")
        or ""
    )


async def _refresh_tariff_ledger(
    hass: HomeAssistant,
    coordinator: GlowmarktDataUpdateCoordinator,
    site_id: str,
) -> None:
    """Persist Glow's effective-dated tariff history for each available cost resource."""
    ledger: dict[str, list[dict[str, Any]]] = {}
    for commodity, (cost_classifier, _, _, _) in COMPONENT_KEYS.items():
        resource = coordinator.resources.get(cost_classifier)
        if not resource:
            continue
        data = await coordinator.api_client._get_json(  # noqa: SLF001
            f"{GLOWMARKT_API_BASE}/resource/{resource['resource_id']}/tariff-list"
        )
        if not isinstance(data, dict) or data.get("status") not in (None, "OK"):
            raise GlowmarktApiError("Glowmarkt tariff-list query returned an error")
        raw = data.get("data") or []
        if isinstance(raw, list):
            ledger[commodity] = sorted(
                [item for item in raw if isinstance(item, dict)],
                key=_effective_key,
            )

    store = Store(
        hass,
        TARIFF_HISTORY_STORAGE_VERSION,
        f"{DOMAIN}_{site_id}_tariff_history",
    )
    await store.async_save(
        {
            "updated_at": datetime.now(UK_TZ).isoformat(),
            "commodities": ledger,
        }
    )


async def async_cost_history_worker(
    hass: HomeAssistant,
    coordinator: GlowmarktDataUpdateCoordinator,
    site_id: str,
) -> None:
    """Backfill Energy/dashboard cost history, then keep tariff history current."""
    await asyncio.sleep(INITIAL_DELAY_SECONDS)

    try:
        await _backfill_cost_statistics(hass, coordinator, site_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        _LOGGER.exception("Glowmarkt cost-history backfill failed")

    while True:
        try:
            await _refresh_tariff_ledger(hass, coordinator, site_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Glowmarkt tariff-history refresh failed")
        await asyncio.sleep(TARIFF_REFRESH_SECONDS)
