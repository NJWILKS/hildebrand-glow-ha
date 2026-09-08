"""Migrate Home Assistant Energy preferences to stable external statistics."""
from __future__ import annotations

from copy import deepcopy

from homeassistant.components.energy.data import async_get_manager
from homeassistant.core import HomeAssistant


async def async_migrate_energy_consumption_statistics(
    hass: HomeAssistant,
    legacy_to_external: dict[str, str],
) -> bool:
    """Replace only exact Hildebrand legacy consumption statistic references.

    Earlier releases used the live sensor's Recorder statistic for both imported
    history and current-day data. 2.3.4 gives the integration a single external
    statistic owner instead. Existing Energy-dashboard configuration is migrated
    in place so users do not have to remove/re-add their grid or gas source.
    """
    if not legacy_to_external:
        return False

    manager = await async_get_manager(hass)
    if manager.data is None:
        return False

    sources = deepcopy(manager.data.get("energy_sources", []))
    changed = False

    for source in sources:
        # Current unified Energy source schemas use stat_energy_from directly.
        current = source.get("stat_energy_from")
        if isinstance(current, str) and current in legacy_to_external:
            source["stat_energy_from"] = legacy_to_external[current]
            changed = True

        # Be conservative with an old, not-yet-migrated grid preference shape.
        flow_from = source.get("flow_from")
        if isinstance(flow_from, list):
            for flow in flow_from:
                if not isinstance(flow, dict):
                    continue
                current = flow.get("stat_energy_from")
                if isinstance(current, str) and current in legacy_to_external:
                    flow["stat_energy_from"] = legacy_to_external[current]
                    changed = True

    if changed:
        await manager.async_update({"energy_sources": sources})
    return changed
