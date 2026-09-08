"""Rolling and completed-day cost ingestion for Hildebrand Glow."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_import_statistics,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .api import UK_TZ
from .cost_history import (
    COMPONENT_KEYS,
    COST_BACKFILL_SCHEMA_VERSION,
    COST_HISTORY_REFRESH_SECONDS,
    COST_HISTORY_STORAGE_VERSION,
    INITIAL_DELAY_SECONDS,
    TARIFF_HISTORY_STORAGE_VERSION,
    TARIFF_REFRESH_SECONDS,
    _clear_statistics,
    _external_cost_metadata,
    _load_tariff_ledger,
    _metadata,
    _refresh_tariff_ledger,
    _save_tariff_analysis,
    build_component_statistics,
    build_total_cost_statistics,
    energy_cost_statistic_id,
)
from .costing import CostBreakdown, get_cost_history
from .tariff import derive_tariff_periods, normalise_cost_history, tariff_period_as_dict

if TYPE_CHECKING:
    from .coordinator import GlowmarktDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# v2.3 makes the completed/open-day boundary explicit. The old v2.1/v2.2
# cost store inferred completion from Recorder's last external row, which is not
# valid once the current day is also imported provisionally.
COST_INGESTION_SCHEMA_VERSION = 1


def _day_start(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UK_TZ)


def _next_day(day: str) -> datetime:
    return _day_start(day) + timedelta(days=1)


def _yesterday(now_uk: datetime) -> str:
    today = now_uk.replace(hour=0, minute=0, second=0, microsecond=0)
    return (today - timedelta(days=1)).date().isoformat()


def _settled_prefix(history: list[CostBreakdown]) -> list[CostBreakdown]:
    """Return only the contiguous completed prefix whose P1D total is available."""
    settled: list[CostBreakdown] = []
    for breakdown in history:
        if breakdown.standing_charge_status == "daily_pending":
            break
        settled.append(breakdown)
    return settled


async def _entity_id(
    hass: HomeAssistant,
    site_id: str,
    sensor_key: str,
) -> str | None:
    """Resolve one component sensor without waiting during a normal poll."""
    from homeassistant.helpers import entity_registry as er

    from .const import DOMAIN
    from .identity import sensor_unique_id

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
        f"hildebrand_glow_{site_id}_cost_history",
    )
    return store, await store.async_load() or {}


async def _reconcile_locked(
    hass: HomeAssistant,
    coordinator: GlowmarktDataUpdateCoordinator,
    site_id: str,
    tariff_ledger: dict[str, Any] | None,
) -> bool:
    """Reconcile every available completed day and return whether history is usable."""
    store, state = await _load_store(hass, site_id)
    schema_current = state.get("_ingestion_version") == COST_INGESTION_SCHEMA_VERSION
    full_backfill = not schema_current
    commodity_state = state.setdefault("commodities", {})
    imported: dict[str, int] = {}
    tariff_analysis: dict[str, list[dict[str, Any]]] = {}

    if tariff_ledger is None:
        tariff_ledger = await _load_tariff_ledger(hass, site_id)
    tariff_rows_by_commodity = tariff_ledger.get("commodities", {})

    for commodity, (
        cost_classifier,
        _daily_key,
        usage_key,
        standing_key,
    ) in COMPONENT_KEYS.items():
        resource = coordinator.resources.get(cost_classifier)
        if not resource:
            continue

        usage_entity = await _entity_id(hass, site_id, usage_key)
        standing_entity = await _entity_id(hass, site_id, standing_key)
        if usage_entity is None or standing_entity is None:
            _LOGGER.debug(
                "Cost component entities not registered for %s; reconciliation deferred",
                commodity,
            )
            return False

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

        history = await get_cost_history(
            coordinator.api_client,
            resource["resource_id"],
            start_uk=start_uk,
        )
        history = _settled_prefix(history)

        # A current schema with no newly settled day is already safe to use if
        # its completed marker reaches yesterday. This is the normal daytime path.
        if not history and not full_backfill:
            continue
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

        total_stats = build_total_cost_statistics(history, baseline=total_baseline)
        usage_stats, standing_stats = build_component_statistics(
            history,
            usage_baseline=usage_baseline,
            standing_baseline=standing_baseline,
        )
        external_id = energy_cost_statistic_id(site_id, commodity)

        if total_stats:
            if full_backfill:
                # The external statistic is 100% integration-owned, so a schema
                # change can safely rebuild it and remove provisional/legacy rows.
                await _clear_statistics(hass, [external_id])
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
            "Reconciled Glowmarkt completed cost days: %s",
            ", ".join(
                f"{commodity}={days} day(s)" for commodity, days in imported.items()
            ),
        )
    return True


async def _import_open_day_locked(
    hass: HomeAssistant,
    coordinator: GlowmarktDataUpdateCoordinator,
    site_id: str,
    now_uk: datetime,
) -> None:
    """Overwrite provisional current-day external cost rows from PT30M snapshots."""
    _store, state = await _load_store(hass, site_id)
    if state.get("_ingestion_version") != COST_INGESTION_SCHEMA_VERSION:
        return

    today = now_uk.date().isoformat()
    yesterday = _yesterday(now_uk)
    commodity_state = state.get("commodities", {})

    for commodity, (cost_classifier, _daily_key, _usage_key, _standing_key) in (
        COMPONENT_KEYS.items()
    ):
        resource = coordinator.resources.get(cost_classifier)
        if not resource:
            continue
        previous = commodity_state.get(commodity, {})

        # Never build today's cumulative sum on top of an unreconciled yesterday.
        # The sensor still shows today's PT30M API value; the Energy cost statistic
        # waits until its historical baseline is trustworthy.
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

        baseline = float(previous.get("completed_total_sum_gbp", 0.0))
        stats = build_total_cost_statistics([breakdown], baseline=baseline)
        if not stats:
            continue
        async_add_external_statistics(
            hass,
            _external_cost_metadata(site_id, commodity),
            stats,
        )
        _LOGGER.debug(
            "Imported %s provisional cost intervals for %s (%s)",
            len(breakdown.usage_intervals),
            commodity,
            today,
        )


async def async_sync_cost_ingestion(
    hass: HomeAssistant,
    coordinator: GlowmarktDataUpdateCoordinator,
    site_id: str,
    *,
    tariff_ledger: dict[str, Any] | None = None,
) -> None:
    """Reconcile closed days first, then publish the open day's PT30M cost."""
    async with coordinator.cost_history_lock:
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
            )


async def async_cost_ingestion_worker(
    hass: HomeAssistant,
    coordinator: GlowmarktDataUpdateCoordinator,
    site_id: str,
) -> None:
    """Prime cost history after setup and provide a low-frequency safety sync."""
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
