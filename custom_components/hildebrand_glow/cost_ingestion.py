"""Tariff-first cost ingestion for Hildebrand Glow."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .api import HISTORY_INTERVAL_DAYS, DailyReading, GlowmarktApiError, UK_TZ
from .const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_ELECTRICITY_COST,
    CLASSIFIER_GAS_CONSUMPTION,
    CLASSIFIER_GAS_COST,
    DOMAIN,
    GLOWMARKT_API_BASE,
)
from .costing import CostBreakdown, get_cost_history
from .identity import sensor_unique_id
from .tariff import derive_tariff_periods, tariff_period_as_dict
from .tariff_costing import price_cost_history

if TYPE_CHECKING:
    from .coordinator import GlowmarktDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

COST_HISTORY_STORAGE_VERSION = 1
TARIFF_HISTORY_STORAGE_VERSION = 1
COST_BACKFILL_SCHEMA_VERSION = 8
COST_INGESTION_SCHEMA_VERSION = 3
INITIAL_DELAY_SECONDS = 30
COST_HISTORY_REFRESH_SECONDS = 6 * 60 * 60
TARIFF_REFRESH_SECONDS = 24 * 60 * 60
STAT_PRECISION = 6
RECONCILIATION_WARNING_PENCE = 5.0

COMPONENT_KEYS = {
    "electricity": (
        CLASSIFIER_ELECTRICITY_COST,
        CLASSIFIER_ELECTRICITY_CONSUMPTION,
        "electricity_daily_cost",
        "electricity_usage_cost",
        "electricity_standing_charge",
    ),
    "gas": (
        CLASSIFIER_GAS_COST,
        CLASSIFIER_GAS_CONSUMPTION,
        "gas_daily_cost",
        "gas_usage_cost",
        "gas_standing_charge",
    ),
}


def _round_stat(value: float) -> float:
    return round(float(value), STAT_PRECISION)


def _gbp_from_pence(value: float) -> float:
    return _round_stat(float(value) / 100.0)


def _safe_stat_part(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")


def energy_cost_statistic_id(site_id: str, commodity: str) -> str:
    """Return the stable external Energy total-cost statistic ID."""
    return f"{DOMAIN}:{_safe_stat_part(site_id)}_{_safe_stat_part(commodity)}_energy_cost"


def cost_component_statistic_id(site_id: str, commodity: str, component: str) -> str:
    """Return a stable external usage-cost or standing-charge statistic ID."""
    if component not in {"usage_cost", "standing_charge"}:
        raise ValueError(f"Unsupported cost component: {component}")
    return (
        f"{DOMAIN}:{_safe_stat_part(site_id)}_{_safe_stat_part(commodity)}_"
        f"{component}"
    )


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


def _external_component_metadata(
    site_id: str,
    commodity: str,
    component: str,
) -> StatisticMetaData:
    return StatisticMetaData(
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name=f"Hildebrand Glow {commodity.title()} {component.replace('_', ' ').title()}",
        source=DOMAIN,
        statistic_id=cost_component_statistic_id(site_id, commodity, component),
        unit_class=None,
        unit_of_measurement="GBP",
    )


def _day_start_utc(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UK_TZ).astimezone(timezone.utc)


def _hourly_pence(breakdown: CostBreakdown) -> dict[datetime, float]:
    hourly: dict[datetime, float] = {}
    for timestamp, value_pence in breakdown.usage_intervals:
        hour_start = timestamp.replace(minute=0, second=0, microsecond=0)
        hourly[hour_start] = hourly.get(hour_start, 0.0) + float(value_pence)
    return hourly


def build_total_cost_statistics(
    history: list[CostBreakdown],
    *,
    baseline: float = 0.0,
) -> list[StatisticData]:
    """Build hourly total-cost rows with a monotonic cumulative Energy sum."""
    running = _round_stat(baseline)
    stats: list[StatisticData] = []

    for breakdown in history:
        total_gbp = _gbp_from_pence(breakdown.total_pence)
        hourly_pence = _hourly_pence(breakdown)

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
            # The standing charge is a daily cost. Put it into the first published
            # hour so the total-cost Energy series adds it once and only once.
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
    """Build stackable daily usage-cost and standing-charge statistics.

    ``state`` is the value a daily bar should display. Usage state accumulates
    through the UK-local day; standing state is the one fixed daily charge. ``sum``
    remains monotonic across days for Recorder's long-term statistics model.
    """
    usage_running = _round_stat(usage_baseline)
    standing_running = _round_stat(standing_baseline)
    usage_stats: list[StatisticData] = []
    standing_stats: list[StatisticData] = []

    for breakdown in history:
        hourly_pence = _hourly_pence(breakdown)
        ordered_hours = sorted(hourly_pence)
        if not ordered_hours:
            ordered_hours = [_day_start_utc(breakdown.day)]

        if breakdown.usage_pence is not None:
            target_usage = _gbp_from_pence(breakdown.usage_pence)
            if hourly_pence:
                increments = [_gbp_from_pence(hourly_pence[hour]) for hour in ordered_hours]
                correction = _round_stat(target_usage - sum(increments))
                if correction:
                    increments[-1] = _round_stat(increments[-1] + correction)
            else:
                increments = [target_usage]

            day_state = 0.0
            for hour_start, increment in zip(ordered_hours, increments, strict=True):
                day_state = _round_stat(day_state + increment)
                usage_running = _round_stat(usage_running + increment)
                usage_stats.append(
                    StatisticData(
                        start=hour_start,
                        state=day_state,
                        sum=usage_running,
                    )
                )

        if breakdown.standing_charge_pence is not None:
            standing_gbp = _gbp_from_pence(breakdown.standing_charge_pence)
            standing_running = _round_stat(standing_running + standing_gbp)
            for hour_start in ordered_hours:
                standing_stats.append(
                    StatisticData(
                        start=hour_start,
                        state=standing_gbp,
                        sum=standing_running,
                    )
                )

    return usage_stats, standing_stats


async def _clear_statistics(hass: HomeAssistant, statistic_ids: list[str]) -> None:
    if not statistic_ids:
        return
    done = asyncio.Event()

    def _done() -> None:
        hass.loop.call_soon_threadsafe(done.set)

    get_instance(hass).async_clear_statistics(statistic_ids, on_done=_done)
    await done.wait()


async def _entity_id(
    hass: HomeAssistant,
    site_id: str,
    sensor_key: str,
) -> str | None:
    """Resolve a legacy entity-backed component statistic for cleanup only."""
    registry = er.async_get(hass)
    return registry.async_get_entity_id(
        "sensor",
        DOMAIN,
        sensor_unique_id(site_id, sensor_key, None),
    )


async def _load_store(hass: HomeAssistant, site_id: str) -> tuple[Store, dict[str, Any]]:
    store = Store(
        hass,
        COST_HISTORY_STORAGE_VERSION,
        f"{DOMAIN}_{site_id}_cost_history",
    )
    return store, await store.async_load() or {}


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
    analysis: dict[str, Any],
) -> None:
    store = Store(
        hass,
        TARIFF_HISTORY_STORAGE_VERSION,
        f"{DOMAIN}_{site_id}_tariff_history",
    )
    state = await store.async_load() or {}
    state["analysis"] = analysis
    await store.async_save(state)


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
    """Fetch and persist the effective-dated Glow tariff ledger first."""
    ledger: dict[str, list[dict[str, Any]]] = {}
    for commodity, (
        cost_classifier,
        _consumption_classifier,
        _daily,
        _usage,
        _standing,
    ) in COMPONENT_KEYS.items():
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


def _day_start(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UK_TZ)


def _next_day(day: str) -> datetime:
    return _day_start(day) + timedelta(days=1)


def _yesterday(now_uk: datetime) -> str:
    today = now_uk.replace(hour=0, minute=0, second=0, microsecond=0)
    return (today - timedelta(days=1)).date().isoformat()


def _settled_prefix(history: list[CostBreakdown]) -> list[CostBreakdown]:
    """Stop at the first completed day for which Glow has not published P1D yet."""
    settled: list[CostBreakdown] = []
    for breakdown in history:
        if breakdown.standing_charge_status == "daily_pending":
            break
        settled.append(breakdown)
    return settled


async def _consumption_history(
    coordinator: GlowmarktDataUpdateCoordinator,
    resource_id: str,
    *,
    start_uk: datetime | None,
) -> list[DailyReading]:
    """Fetch consumption for the same window being costed."""
    if start_uk is None:
        return await coordinator.api_client.get_available_daily_readings(resource_id)

    start_uk = start_uk.astimezone(UK_TZ).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    today_start = datetime.now(UK_TZ).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    result: list[DailyReading] = []
    chunk_start = start_uk
    while chunk_start < today_start:
        chunk_end = min(
            chunk_start + timedelta(days=HISTORY_INTERVAL_DAYS),
            today_start,
        )
        result.extend(
            await coordinator.api_client._fetch_history_chunk(  # noqa: SLF001
                resource_id,
                chunk_start,
                chunk_end,
            )
        )
        chunk_start = chunk_end
    return result


def _log_pricing_reconciliation(
    commodity: str,
    diagnostics: list[dict[str, Any]],
) -> None:
    for item in diagnostics:
        if not item.get("complete_day"):
            continue
        delta = item.get("glow_reconciliation_delta_pence")
        if delta is None:
            continue
        if abs(float(delta)) > RECONCILIATION_WARNING_PENCE:
            _LOGGER.warning(
                "Tariff-priced %s cost differs from Glow P1D by %.3fp on %s "
                "(usage=%s, standing=%s)",
                commodity,
                float(delta),
                item.get("day"),
                item.get("usage_source"),
                item.get("standing_source"),
            )


async def _reconcile_locked(
    hass: HomeAssistant,
    coordinator: GlowmarktDataUpdateCoordinator,
    site_id: str,
    tariff_ledger: dict[str, Any] | None,
) -> bool:
    """Reconcile every available completed day using tariff-first components."""
    store, state = await _load_store(hass, site_id)
    full_backfill = state.get("_ingestion_version") != COST_INGESTION_SCHEMA_VERSION
    commodity_state = state.setdefault("commodities", {})
    imported: dict[str, int] = {}
    tariff_analysis: dict[str, Any] = {}

    if tariff_ledger is None:
        tariff_ledger = await _load_tariff_ledger(hass, site_id)
    tariff_rows_by_commodity = tariff_ledger.get("commodities", {})

    for commodity, (
        cost_classifier,
        consumption_classifier,
        _daily_key,
        usage_key,
        standing_key,
    ) in COMPONENT_KEYS.items():
        cost_resource = coordinator.resources.get(cost_classifier)
        if not cost_resource:
            continue
        consumption_resource = coordinator.resources.get(consumption_classifier)

        previous = commodity_state.get(commodity, {}) if not full_backfill else {}
        completed_day = previous.get("last_completed_day") or previous.get("last_day")
        total_baseline = float(
            previous.get(
                "completed_total_sum_gbp",
                previous.get("total_sum_gbp", 0.0),
            )
        )
        usage_baseline = float(previous.get("usage_sum_gbp", 0.0))
        standing_baseline = float(previous.get("standing_sum_gbp", 0.0))
        start_uk = _next_day(completed_day) if completed_day and not full_backfill else None

        raw_history = await get_cost_history(
            coordinator.api_client,
            cost_resource["resource_id"],
            start_uk=start_uk,
        )
        raw_history = _settled_prefix(raw_history)
        if not raw_history:
            continue

        configured_standing = coordinator.tariff_config.get(
            f"{commodity}_standing_charge"
        )
        configured_rate = coordinator.tariff_config.get(f"{commodity}_rate")
        rows = tariff_rows_by_commodity.get(commodity, [])
        periods = derive_tariff_periods(
            rows if isinstance(rows, list) else [],
            raw_history,
            configured_standing_gbp=configured_standing,
            configured_rate_gbp_per_kwh=configured_rate,
        )

        consumption: list[DailyReading] = []
        if consumption_resource is not None:
            consumption = await _consumption_history(
                coordinator,
                consumption_resource["resource_id"],
                start_uk=start_uk,
            )

        history, pricing = price_cost_history(raw_history, consumption, periods)
        _log_pricing_reconciliation(commodity, pricing)
        tariff_analysis[commodity] = {
            "periods": [tariff_period_as_dict(item) for item in periods],
            "pricing": pricing,
        }

        total_stats = build_total_cost_statistics(history, baseline=total_baseline)
        usage_stats, standing_stats = build_component_statistics(
            history,
            usage_baseline=usage_baseline,
            standing_baseline=standing_baseline,
        )
        total_external_id = energy_cost_statistic_id(site_id, commodity)
        usage_external_id = cost_component_statistic_id(
            site_id, commodity, "usage_cost"
        )
        standing_external_id = cost_component_statistic_id(
            site_id, commodity, "standing_charge"
        )

        if full_backfill:
            legacy_component_ids = [
                statistic_id
                for statistic_id in (
                    await _entity_id(hass, site_id, usage_key),
                    await _entity_id(hass, site_id, standing_key),
                )
                if statistic_id is not None
            ]
            await _clear_statistics(
                hass,
                [
                    total_external_id,
                    usage_external_id,
                    standing_external_id,
                    *legacy_component_ids,
                ],
            )

        if total_stats:
            async_add_external_statistics(
                hass,
                _external_cost_metadata(site_id, commodity),
                total_stats,
            )
        if usage_stats:
            async_add_external_statistics(
                hass,
                _external_component_metadata(site_id, commodity, "usage_cost"),
                usage_stats,
            )
        if standing_stats:
            async_add_external_statistics(
                hass,
                _external_component_metadata(site_id, commodity, "standing_charge"),
                standing_stats,
            )

        commodity_state[commodity] = {
            "last_completed_day": history[-1].day,
            "completed_total_sum_gbp": (
                float(total_stats[-1]["sum"]) if total_stats else total_baseline
            ),
            "usage_sum_gbp": (
                float(usage_stats[-1]["sum"]) if usage_stats else usage_baseline
            ),
            "standing_sum_gbp": (
                float(standing_stats[-1]["sum"])
                if standing_stats
                else standing_baseline
            ),
        }
        imported[commodity] = len(history)

    state["_backfilled"] = True
    state["_backfilled_version"] = COST_BACKFILL_SCHEMA_VERSION
    state["_ingestion_version"] = COST_INGESTION_SCHEMA_VERSION
    await store.async_save(state)
    if tariff_analysis:
        await _save_tariff_analysis(hass, site_id, tariff_analysis)
    if imported:
        _LOGGER.info(
            "Reconciled tariff-priced completed cost days: %s",
            ", ".join(
                f"{commodity}={days} day(s)" for commodity, days in imported.items()
            ),
        )
    return True


async def _open_day_consumption(
    coordinator: GlowmarktDataUpdateCoordinator,
    resource_id: str,
    now_uk: datetime,
) -> DailyReading | None:
    start = now_uk.replace(hour=0, minute=0, second=0, microsecond=0)
    return await coordinator.api_client._fetch_day_reading(  # noqa: SLF001
        resource_id,
        start,
        now_uk,
    )


async def _import_open_day_locked(
    hass: HomeAssistant,
    coordinator: GlowmarktDataUpdateCoordinator,
    site_id: str,
    now_uk: datetime,
    tariff_ledger: dict[str, Any],
) -> None:
    """Publish today's tariff-priced usage and standing-charge stack."""
    _store, state = await _load_store(hass, site_id)
    if state.get("_ingestion_version") != COST_INGESTION_SCHEMA_VERSION:
        return

    today = now_uk.date().isoformat()
    yesterday = _yesterday(now_uk)
    commodity_state = state.get("commodities", {})
    tariff_rows_by_commodity = tariff_ledger.get("commodities", {})

    for commodity, (
        cost_classifier,
        consumption_classifier,
        _daily,
        _usage_key,
        _standing_key,
    ) in COMPONENT_KEYS.items():
        if cost_classifier not in coordinator.resources:
            continue
        previous = commodity_state.get(commodity, {})
        if previous.get("last_completed_day") != yesterday:
            continue

        breakdown = coordinator.cost_breakdowns.get(commodity)
        if (
            breakdown is None
            or breakdown.complete_day
            or breakdown.day != today
            or not breakdown.usage_intervals
        ):
            continue

        consumption: list[DailyReading] = []
        consumption_resource = coordinator.resources.get(consumption_classifier)
        if consumption_resource is not None:
            current = await _open_day_consumption(
                coordinator,
                consumption_resource["resource_id"],
                now_uk,
            )
            if current is not None:
                consumption.append(current)

        rows = tariff_rows_by_commodity.get(commodity, [])
        periods = derive_tariff_periods(
            rows if isinstance(rows, list) else [],
            [breakdown],
            configured_standing_gbp=coordinator.tariff_config.get(
                f"{commodity}_standing_charge"
            ),
            configured_rate_gbp_per_kwh=coordinator.tariff_config.get(
                f"{commodity}_rate"
            ),
        )
        priced, pricing = price_cost_history([breakdown], consumption, periods)
        if not priced:
            continue
        open_breakdown = priced[0]

        total_baseline = float(previous.get("completed_total_sum_gbp", 0.0))
        usage_baseline = float(previous.get("usage_sum_gbp", 0.0))
        standing_baseline = float(previous.get("standing_sum_gbp", 0.0))
        total_stats = build_total_cost_statistics([open_breakdown], baseline=total_baseline)
        usage_stats, standing_stats = build_component_statistics(
            [open_breakdown],
            usage_baseline=usage_baseline,
            standing_baseline=standing_baseline,
        )

        if total_stats:
            async_add_external_statistics(
                hass,
                _external_cost_metadata(site_id, commodity),
                total_stats,
            )
        if usage_stats:
            async_add_external_statistics(
                hass,
                _external_component_metadata(site_id, commodity, "usage_cost"),
                usage_stats,
            )
        if standing_stats:
            async_add_external_statistics(
                hass,
                _external_component_metadata(site_id, commodity, "standing_charge"),
                standing_stats,
            )

        if pricing:
            item = pricing[0]
            _LOGGER.debug(
                "Published %s open-day cost stack: rate=%s p/kWh, standing=%s p, "
                "usage_source=%s",
                commodity,
                item.get("unit_rate_pence_per_kwh"),
                item.get("standing_pence"),
                item.get("usage_source"),
            )


async def async_sync_cost_ingestion(
    hass: HomeAssistant,
    coordinator: GlowmarktDataUpdateCoordinator,
    site_id: str,
    *,
    tariff_ledger: dict[str, Any] | None = None,
) -> None:
    """Reconcile tariff-priced closed days, then publish today's stack."""
    async with coordinator.cost_history_lock:
        if tariff_ledger is None:
            tariff_ledger = await _load_tariff_ledger(hass, site_id)
        ready = await _reconcile_locked(
            hass,
            coordinator,
            site_id,
            tariff_ledger,
        )
        if ready:
            await _import_open_day_locked(
                hass,
                coordinator,
                site_id,
                datetime.now(UK_TZ),
                tariff_ledger,
            )


async def async_cost_ingestion_worker(
    hass: HomeAssistant,
    coordinator: GlowmarktDataUpdateCoordinator,
    site_id: str,
) -> None:
    """Fetch tariff first, then continuously reconcile cost statistics."""
    await asyncio.sleep(INITIAL_DELAY_SECONDS)

    tariff_ledger: dict[str, Any] | None = None
    try:
        tariff_ledger = await _refresh_tariff_ledger(hass, coordinator, site_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        _LOGGER.exception("Glowmarkt tariff-history refresh failed")

    try:
        await async_sync_cost_ingestion(
            hass,
            coordinator,
            site_id,
            tariff_ledger=tariff_ledger,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _LOGGER.exception("Glowmarkt cost ingestion sync failed")

    elapsed = 0
    while True:
        await asyncio.sleep(COST_HISTORY_REFRESH_SECONDS)
        elapsed += COST_HISTORY_REFRESH_SECONDS
        tariff_ledger = None
        if elapsed >= TARIFF_REFRESH_SECONDS:
            elapsed = 0
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
            await async_sync_cost_ingestion(
                hass,
                coordinator,
                site_id,
                tariff_ledger=tariff_ledger,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Glowmarkt cost ingestion sync failed")
