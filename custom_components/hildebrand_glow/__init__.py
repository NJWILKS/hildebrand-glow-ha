"""The Hildebrand Glow integration."""
from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import GlowmarktApiClient
from .const import (
    CONF_CONSUMPTION_INTERVAL,
    CONF_COST_INTERVAL,
    CONF_ELECTRICITY_RATE,
    CONF_ELECTRICITY_STANDING_CHARGE,
    CONF_GAS_RATE,
    CONF_GAS_STANDING_CHARGE,
    CONF_VIRTUAL_ENTITY,
    DEFAULT_CONSUMPTION_INTERVAL,
    DEFAULT_COST_INTERVAL,
    DEFAULT_ELECTRICITY_RATE,
    DEFAULT_ELECTRICITY_STANDING_CHARGE,
    DEFAULT_GAS_RATE,
    DEFAULT_GAS_STANDING_CHARGE,
    DOMAIN,
)
from .coordinator import GlowmarktDataUpdateCoordinator
from .energy_migration import async_migrate_energy_consumption_statistics
from .identity import site_identity
from .interval_history import async_interval_history_worker
from .reset import async_cleanup_legacy_statistics

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.SENSOR]


def _entry_value(entry: ConfigEntry, key: str, default):
    return entry.options.get(key, entry.data.get(key, default))


def _tariff_config(entry: ConfigEntry) -> dict[str, float]:
    return {
        "electricity_rate": float(
            _entry_value(entry, CONF_ELECTRICITY_RATE, DEFAULT_ELECTRICITY_RATE)
        ),
        "gas_rate": float(_entry_value(entry, CONF_GAS_RATE, DEFAULT_GAS_RATE)),
        "electricity_standing_charge": float(
            _entry_value(
                entry,
                CONF_ELECTRICITY_STANDING_CHARGE,
                DEFAULT_ELECTRICITY_STANDING_CHARGE,
            )
        ),
        "gas_standing_charge": float(
            _entry_value(
                entry,
                CONF_GAS_STANDING_CHARGE,
                DEFAULT_GAS_STANDING_CHARGE,
            )
        ),
    }


async def _async_cleanup_legacy_statistics_after_setup(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Clear obsolete Recorder statistics without holding up config-entry setup."""
    try:
        await async_cleanup_legacy_statistics(hass, entry)
    except asyncio.CancelledError:
        raise
    except Exception:
        _LOGGER.exception("Failed to clean up legacy Hildebrand statistics")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    session = async_get_clientsession(hass)
    client = GlowmarktApiClient(
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        session=session,
    )
    coordinator = GlowmarktDataUpdateCoordinator(
        hass=hass,
        api_client=client,
        tariff_config=_tariff_config(entry),
        virtual_entity_id=entry.data.get(CONF_VIRTUAL_ENTITY),
        entry_id=entry.entry_id,
        consumption_interval_minutes=int(
            _entry_value(
                entry,
                CONF_CONSUMPTION_INTERVAL,
                DEFAULT_CONSUMPTION_INTERVAL,
            )
        ),
        cost_interval_minutes=int(
            _entry_value(entry, CONF_COST_INTERVAL, DEFAULT_COST_INTERVAL)
        ),
    )

    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Build the new source-of-truth ledger independently of Recorder statistics.
    # The task is owned by the config entry so unload/reload cancels it cleanly.
    entry.async_create_background_task(
        hass,
        async_interval_history_worker(
            hass,
            client,
            coordinator.resources,
            site_identity(
                entry.data.get(CONF_VIRTUAL_ENTITY),
                entry.entry_id,
            ),
        ),
        f"{DOMAIN} PT30M interval history population",
    )

    # 2.3.4 migrated Energy consumption to an external statistic but did not
    # attach the integration-owned external cost statistic to the same Energy
    # source. Re-run the preference repair on every setup so existing installs
    # pick up cost history without requiring the source to be removed/re-added.
    await async_migrate_energy_consumption_statistics(hass, {})

    # 2.3.6 makes visible sensors presentation-only. Earlier versions may have
    # Recorder-owned long-term statistics attached to those entity IDs. Clearing
    # them can legitimately wait behind Recorder work, so it must never block the
    # config-entry setup/bootstrap path. The cleanup touches only legacy sensor IDs;
    # external consumption/cost statistics keep their normal independent owners.
    entry.async_create_background_task(
        hass,
        _async_cleanup_legacy_statistics_after_setup(hass, entry),
        f"{DOMAIN} legacy statistics cleanup",
    )

    coordinator.schedule_history_backfill()
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    coordinator: GlowmarktDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.update_settings(
        _tariff_config(entry),
        int(
            _entry_value(
                entry,
                CONF_CONSUMPTION_INTERVAL,
                DEFAULT_CONSUMPTION_INTERVAL,
            )
        ),
        int(_entry_value(entry, CONF_COST_INTERVAL, DEFAULT_COST_INTERVAL)),
    )
    await coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: GlowmarktDataUpdateCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is not None:
        await coordinator.async_shutdown()

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
