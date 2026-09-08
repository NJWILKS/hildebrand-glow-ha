"""Safe reset helpers for Hildebrand Glow imported history."""
from __future__ import annotations

import asyncio

from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import CONF_VIRTUAL_ENTITY, DOMAIN
from .consumption_statistics import energy_consumption_statistic_id
from .coordinator import CUMULATIVE_STORAGE_VERSION
from .cost_ingestion import (
    COST_HISTORY_STORAGE_VERSION,
    TARIFF_HISTORY_STORAGE_VERSION,
    energy_cost_statistic_id,
)
from .identity import site_identity


def reset_statistic_ids(
    registry: er.EntityRegistry,
    config_entry: ConfigEntry,
    site_id: str,
) -> list[str]:
    """Return all current and legacy Hildebrand statistics for one config entry."""
    # Entity-backed IDs include legacy consumption statistics from <=2.3.3 as well
    # as current monetary sensor statistics. Keeping them in the reset set makes
    # upgrades self-cleaning without touching unrelated Recorder data.
    statistic_ids = {
        entry.entity_id
        for entry in er.async_entries_for_config_entry(registry, config_entry.entry_id)
        if entry.domain == "sensor"
    }
    statistic_ids.update(
        {
            energy_consumption_statistic_id(site_id, "electricity"),
            energy_consumption_statistic_id(site_id, "gas"),
            energy_cost_statistic_id(site_id, "electricity"),
            energy_cost_statistic_id(site_id, "gas"),
        }
    )
    return sorted(statistic_ids)


async def _clear_statistics(hass: HomeAssistant, statistic_ids: list[str]) -> None:
    """Clear statistics and wait for Recorder to finish the queued operation."""
    if not statistic_ids:
        return

    done = asyncio.Event()

    def _done() -> None:
        hass.loop.call_soon_threadsafe(done.set)

    get_instance(hass).async_clear_statistics(statistic_ids, on_done=_done)
    await done.wait()


async def async_reset_imported_history(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
) -> list[str]:
    """Delete only this integration entry's imported statistics and backfill state.

    Entity registry entries, dashboards, credentials and unrelated Recorder history
    are intentionally preserved. A subsequent config-entry setup performs a clean
    backfill from Glow.
    """
    site_id = site_identity(
        config_entry.data.get(CONF_VIRTUAL_ENTITY),
        config_entry.entry_id,
    )
    statistic_ids = reset_statistic_ids(er.async_get(hass), config_entry, site_id)
    await _clear_statistics(hass, statistic_ids)

    stores = (
        Store(
            hass,
            CUMULATIVE_STORAGE_VERSION,
            f"{DOMAIN}_{site_id}_cumulative",
        ),
        Store(
            hass,
            COST_HISTORY_STORAGE_VERSION,
            f"{DOMAIN}_{site_id}_cost_history",
        ),
        Store(
            hass,
            TARIFF_HISTORY_STORAGE_VERSION,
            f"{DOMAIN}_{site_id}_tariff_history",
        ),
    )
    for store in stores:
        await store.async_remove()

    return statistic_ids
