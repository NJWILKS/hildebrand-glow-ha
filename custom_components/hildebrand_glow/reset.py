"""Safe reset and one-time cleanup helpers for Hildebrand Glow statistics."""
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
    cost_component_statistic_id,
    energy_cost_statistic_id,
)
from .identity import site_identity

LEGACY_CLEANUP_STORAGE_VERSION = 1
LEGACY_CLEANUP_SCHEMA_VERSION = 1


def _entry_sensor_statistic_ids(
    registry: er.EntityRegistry,
    config_entry: ConfigEntry,
) -> set[str]:
    """Return sensor entity IDs owned by this config entry."""
    return {
        entry.entity_id
        for entry in er.async_entries_for_config_entry(registry, config_entry.entry_id)
        if entry.domain == "sensor"
    }


def reset_statistic_ids(
    registry: er.EntityRegistry,
    config_entry: ConfigEntry,
    site_id: str,
) -> list[str]:
    """Return all current and legacy Hildebrand statistics for one config entry."""
    statistic_ids = _entry_sensor_statistic_ids(registry, config_entry)
    statistic_ids.update(
        {
            energy_consumption_statistic_id(site_id, "electricity"),
            energy_consumption_statistic_id(site_id, "gas"),
            energy_cost_statistic_id(site_id, "electricity"),
            energy_cost_statistic_id(site_id, "gas"),
            cost_component_statistic_id(site_id, "electricity", "usage_cost"),
            cost_component_statistic_id(site_id, "electricity", "standing_charge"),
            cost_component_statistic_id(site_id, "gas", "usage_cost"),
            cost_component_statistic_id(site_id, "gas", "standing_charge"),
        }
    )
    return sorted(statistic_ids)


def legacy_entity_statistic_ids(
    registry: er.EntityRegistry,
    config_entry: ConfigEntry,
) -> list[str]:
    """Return obsolete Recorder-owned statistics created from visible sensors.

    The 2.3.6 cost schema owns rebuilding its external cost statistics. This cleanup
    therefore touches only entity-backed statistics that can no longer be recreated,
    and deliberately leaves all current external statistics to their normal owners.
    """
    return sorted(_entry_sensor_statistic_ids(registry, config_entry))


async def _clear_statistics(hass: HomeAssistant, statistic_ids: list[str]) -> None:
    """Clear statistics and wait for Recorder to finish the queued operation."""
    if not statistic_ids:
        return

    done = asyncio.Event()

    def _done() -> None:
        hass.loop.call_soon_threadsafe(done.set)

    get_instance(hass).async_clear_statistics(statistic_ids, on_done=_done)
    await done.wait()


async def async_cleanup_legacy_statistics(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
) -> list[str]:
    """Remove obsolete Recorder-owned visible-sensor statistics exactly once.

    All visible sensors are presentation-only from 2.3.6. Earlier releases could
    create long-term Recorder statistics for those entity IDs, so their metadata is
    deleted once. Current integration-owned external statistics are not touched.
    """
    site_id = site_identity(
        config_entry.data.get(CONF_VIRTUAL_ENTITY),
        config_entry.entry_id,
    )
    store = Store(
        hass,
        LEGACY_CLEANUP_STORAGE_VERSION,
        f"{DOMAIN}_{site_id}_statistics_cleanup",
    )
    state = await store.async_load() or {}
    if int(state.get("schema_version", 0) or 0) >= LEGACY_CLEANUP_SCHEMA_VERSION:
        return []

    statistic_ids = legacy_entity_statistic_ids(er.async_get(hass), config_entry)
    await _clear_statistics(hass, statistic_ids)
    await store.async_save({"schema_version": LEGACY_CLEANUP_SCHEMA_VERSION})
    return statistic_ids


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
