"""Historical Glow cost/statistics and tariff-ledger maintenance."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_import_statistics,
    get_last_statistics,
)
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
from .tariff import (
    derive_tariff_periods,
    normalise_cost_history,
    tariff_period_as_dict,
)

_LOGGER = logging.getLogger(__name__)
COST_HISTORY_STORAGE_VERSION = 1
COST_BACKFILL_SCHEMA_VERSION = 5
TARIFF_HISTORY_STORAGE_VERSION = 1
INITIAL_DELAY_SECONDS = 30
ENTITY_WAIT_RETRIES = 6
ENTITY_WAIT_SECONDS = 5
COST_HISTORY_REFRESH_SECONDS = 6 * 60 * 60
TARIFF_REFRESH_SECONDS = 24 * 60 * 60
STAT_PRECISION = 6

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


def _round_stat(value: float) -> float:
    """Keep billing precision in Recorder while avoiding float noise."""
    return round(float(value), STAT_PRECISION)


def _gbp_from_pence(value: float) -> float:
    return _round_stat(float(value) / 100.0)


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


def energy_cost_statistic_id(site_id: str, commodity: str) -> str:
    """Return a stable Home Assistant external statistic ID for Energy cost."""
    safe_site = re.sub(r"[^a-z0-9_]+", "_", site_id.lower()).strip("_")
    safe_commodity = re.sub(r"[^a-z0-9_]+", "_", commodity.lower()).strip("_")
    return f"{DOMAIN}:{safe_site}_{safe_commodity}_energy_cost"


def _external_cost_metadata(site_id: str, commodity: str) -> StatisticMetaData:
    return StatisticMetaData(
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name=f"Hildebrand Glow {commodity.title()} Energy Cost",
        source=DOMAIN,
        statistic_id=energy_cost_statistic_id(site_id, commodity),
        unit_class=None,
        unit_of_measurement="GBP",
    )


def _day_start_utc(day: str) -> datetime:
    return (
        datetime.fromisoformat(day)
        .replace(tzinfo=UK_TZ)
        .astimezone(timezone.utc)
    )


def _next_day_start_uk(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UK_TZ) + timedelta(days=1)


def build_total_cost_statistics(
    history: list[CostBreakdown],
    *,
    baseline: float = 0.0,
) -> list[StatisticData]:
    """Build authoritative cumulative cost statistics for the Energy dashboard.

    Historical PT30M cost preserves the usage-cost shape. Any difference between
    those intervals and the authoritative completed P1D total is applied to the
    first hour of that UK-local day. Values retain sub-penny precision in Recorder;
    the UI is free to display them rounded to normal currency precision.
    """
    running = _round_stat(baseline)
    stats: list[StatisticData] = []

    for breakdown in history:
        total_gbp = _gbp_from_pence(breakdown.total_pence)
        hourly_pence: dict[datetime, float] = {}
        for timestamp, value_pence in breakdown.usage_intervals:
            hour_start = timestamp.replace(minute=0, second=0, microsecond=0)
            hourly_pence[hour_start] = (
                hourly_pence.get(hour_start, 0.0) + float(value_pence)
            )

        if not hourly_pence:
            running = _round_stat(running + total_gbp)
            stats.append(
                StatisticData(
                    start=_day_start_utc(breakdown.day),
                    state=total_gbp,
                    sum=running,
                )
            )
            continue

        ordered_hours = sorted(hourly_pence)
        residual_pence = float(breakdown.total_pence) - sum(hourly_pence.values())
        if residual_pence:
            hourly_pence[ordered_hours[0]] += residual_pence

        day_states = [_gbp_from_pence(hourly_pence[hour]) for hour in ordered_hours]
        correction = _round_stat(total_gbp - sum(day_states))
        if correction:
            day_states[-1] = _round_stat(day_states[-1] + correction)

        for hour_start, state in zip(ordered_hours, day_states, strict=True):
            running = _round_stat(running + state)
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
    *,
    usage_baseline: float = 0.0,
    standing_baseline: float = 0.0,
) -> tuple[list[StatisticData], list[StatisticData]]:
    """Build daily usage/standing states with precise cumulative sums."""
    usage_running = _round_stat(usage_baseline)
    standing_running = _round_stat(standing_baseline)
    usage_stats: list[StatisticData] = []
    standing_stats: list[StatisticData] = []

    for breakdown in history:
        start = _day_start_utc(breakdown.day)

        if breakdown.usage_pence is not None:
            usage_gbp = _gbp_from_pence(breakdown.usage_pence)
            usage_running = _round_stat(usage_running + usage_gbp)
            usage_stats.append(
                StatisticData(
                    start=start,
                    state=usage_gbp,
                    sum=usage_running,
                )
            )

        if breakdown.standing_charge_pence is not None:
            standing_gbp = _gbp_from_pence(breakdown.standing_charge_pence)
            standing_running = _round_stat(standing_running + standing_gbp)
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


def _last_sum(stats: list[StatisticData], fallback: float) -> float:
    if not stats:
        return _round_stat(fallback)
    value = stats[-1].get("sum")
    return _round_stat(float(value)) if value is not None else _round_stat(fallback)


def _trim_trailing_pending(history: list[CostBreakdown]) -> list[CostBreakdown]:
    """Do not finalise recent days until Glow has published the P1D bucket."""
    settled = list(history)
    while settled and settled[-1].standing_charge_status == "daily_pending":
        settled.pop()
    return settled


async def _clear_statistics(hass: HomeAssistant, statistic_ids: list[str]) -> None:
    """Clear integration-owned statistics and wait for Recorder to finish."""
    if not statistic_ids:
        return
    done = asyncio.Event()

    def _done() -> None:
        hass.loop.call_soon_threadsafe(done.set)

    get_instance(hass).async_clear_statistics(statistic_ids, on_done=_done)
    await done.wait()


async def _last_external_stat(
    hass: HomeAssistant,
    statistic_id: str,
) -> dict[str, Any] | None:
    """Read the last persisted external statistic directly from Recorder."""
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics,
        hass,
        1,
        statistic_id,
        True,
        {"sum"},
    )
    rows = last.get(statistic_id) if last else None
    return rows[0] if rows else None


def _external_resume_point(
    last_stat: dict[str, Any] | None,
) -> tuple[datetime | None, float]:
    """Return next UK-local day and cumulative sum from Recorder's last row."""
    if not last_stat:
        return None, 0.0

    sum_value = last_stat.get("sum")
    baseline = _round_stat(float(sum_value)) if sum_value is not None else 0.0
    start = last_stat.get("start")
    if start is None:
        return None, baseline

    last_local = datetime.fromtimestamp(
        float(start),
        tz=timezone.utc,
    ).astimezone(UK_TZ)
    next_day = last_local.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ) + timedelta(days=1)
    return next_day, baseline


async def _load_tariff_ledger(hass: HomeAssistant, site_id: str) -> dict[str, Any]:
    store = Store(
        hass,
        TARIFF_HISTORY_STORAGE_VERSION,
        f"{DOMAIN}_{site_id}_tariff_history",
    )
    return await store.async_load() or {}


async def _save_tariff_analysis(
    hass: HomeAssistant,
    site_id: str,
    analysis: dict[str, list[dict[str, Any]]],
) -> None:
    store = Store(
        hass,
        TARIFF_HISTORY_STORAGE_VERSION,
        f"{DOMAIN}_{site_id}_tariff_history",
    )
    state = await store.async_load() or {}
    state["analysis"] = analysis
    await store.async_save(state)


async def _sync_cost_statistics(
    hass: HomeAssistant,
    coordinator: GlowmarktDataUpdateCoordinator,
    site_id: str,
    tariff_ledger: dict[str, Any] | None = None,
) -> None:
    """Initial-backfill and then incrementally extend cost statistics."""
    store = Store(
        hass,
        COST_HISTORY_STORAGE_VERSION,
        f"{DOMAIN}_{site_id}_cost_history",
    )
    state = await store.async_load() or {}
    full_backfill = state.get("_backfilled_version") != COST_BACKFILL_SCHEMA_VERSION
    commodity_state = state.setdefault("commodities", {})
    imported: dict[str, int] = {}
    tariff_analysis: dict[str, list[dict[str, Any]]] = {}

    if tariff_ledger is None:
        tariff_ledger = await _load_tariff_ledger(hass, site_id)
    tariff_rows_by_commodity = tariff_ledger.get("commodities", {})

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

        external_id = energy_cost_statistic_id(site_id, commodity)
        previous = commodity_state.get(commodity, {}) if not full_backfill else {}
        rebuild_external = full_backfill
        total_baseline = 0.0
        start_uk: datetime | None = None

        if not rebuild_external:
            last_external = await _last_external_stat(hass, external_id)
            start_uk, total_baseline = _external_resume_point(last_external)
            if last_external is None:
                rebuild_external = True
                previous = {}
                start_uk = None
                total_baseline = 0.0

        history = await get_cost_history(
            coordinator.api_client,
            resource["resource_id"],
            start_uk=start_uk,
        )
        history = _trim_trailing_pending(history)
        if not history:
            continue

        configured_standing = coordinator.tariff_config.get(
            f"{commodity}_standing_charge"
        )
        configured_rate = coordinator.tariff_config.get(f"{commodity}_rate")
        rows = tariff_rows_by_commodity.get(commodity, [])
        periods = derive_tariff_periods(
            rows if isinstance(rows, list) else [],
            history,
            configured_standing_gbp=configured_standing,
            configured_rate_gbp_per_kwh=configured_rate,
        )
        tariff_analysis[commodity] = [tariff_period_as_dict(item) for item in periods]
        history = normalise_cost_history(history, periods)

        usage_baseline = float(previous.get("usage_sum_gbp", 0.0))
        standing_baseline = float(previous.get("standing_sum_gbp", 0.0))

        total_stats = build_total_cost_statistics(
            history,
            baseline=total_baseline,
        )
        usage_stats, standing_stats = build_component_statistics(
            history,
            usage_baseline=usage_baseline,
            standing_baseline=standing_baseline,
        )

        if total_stats:
            if rebuild_external:
                await _clear_statistics(hass, [external_id])
            async_import_statistics(hass, _metadata(daily_entity), total_stats)
            async_add_external_statistics(
                hass,
                _external_cost_metadata(site_id, commodity),
                total_stats,
            )
        if usage_stats:
            async_import_statistics(hass, _metadata(usage_entity), usage_stats)
        if standing_stats:
            async_import_statistics(hass, _metadata(standing_entity), standing_stats)

        commodity_state[commodity] = {
            "last_day": history[-1].day,
            "total_sum_gbp": _last_sum(total_stats, total_baseline),
            "usage_sum_gbp": _last_sum(usage_stats, usage_baseline),
            "standing_sum_gbp": _last_sum(standing_stats, standing_baseline),
        }
        imported[commodity] = len(history)

    state["_backfilled"] = True
    state["_backfilled_version"] = COST_BACKFILL_SCHEMA_VERSION
    await store.async_save(state)
    if tariff_analysis:
        await _save_tariff_analysis(hass, site_id, tariff_analysis)
    if imported:
        _LOGGER.info(
            "Synced Glowmarkt Energy cost totals/components: %s",
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
) -> dict[str, Any]:
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

    result: dict[str, Any] = {
        "updated_at": datetime.now(UK_TZ).isoformat(),
        "commodities": ledger,
    }
    store = Store(
        hass,
        TARIFF_HISTORY_STORAGE_VERSION,
        f"{DOMAIN}_{site_id}_tariff_history",
    )
    existing = await store.async_load() or {}
    if "analysis" in existing:
        result["analysis"] = existing["analysis"]
    await store.async_save(result)
    return result


async def async_cost_history_worker(
    hass: HomeAssistant,
    coordinator: GlowmarktDataUpdateCoordinator,
    site_id: str,
) -> None:
    """Maintain Energy cost statistics and the effective-dated tariff ledger."""
    await asyncio.sleep(INITIAL_DELAY_SECONDS)

    tariff_ledger: dict[str, Any] | None = None
    try:
        tariff_ledger = await _refresh_tariff_ledger(hass, coordinator, site_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        _LOGGER.exception("Glowmarkt tariff-history refresh failed")

    try:
        await _sync_cost_statistics(hass, coordinator, site_id, tariff_ledger)
    except asyncio.CancelledError:
        raise
    except Exception:
        _LOGGER.exception("Glowmarkt cost-history sync failed")

    cost_refreshes = 0
    while True:
        await asyncio.sleep(COST_HISTORY_REFRESH_SECONDS)
        cost_refreshes += 1
        tariff_ledger = None
        if cost_refreshes * COST_HISTORY_REFRESH_SECONDS >= TARIFF_REFRESH_SECONDS:
            cost_refreshes = 0
            try:
                tariff_ledger = await _refresh_tariff_ledger(
                    hass,
                    coordinator,
                    site_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("Glowmarkt tariff-history refresh failed")

        try:
            await _sync_cost_statistics(hass, coordinator, site_id, tariff_ledger)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Glowmarkt cost-history sync failed")
