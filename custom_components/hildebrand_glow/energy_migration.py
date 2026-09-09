"""Migrate Home Assistant Energy preferences to stable external statistics."""
from __future__ import annotations

from copy import deepcopy

from homeassistant.components.energy.data import async_get_manager
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_ENERGY_CONSUMPTION_SUFFIX = "_energy_consumption"
_ENERGY_COST_SUFFIX = "_energy_cost"


def _matching_cost_statistic(consumption_statistic: object) -> str | None:
    """Return this integration's matching cost statistic for an Energy source."""
    if not isinstance(consumption_statistic, str):
        return None
    prefix = f"{DOMAIN}:"
    if not consumption_statistic.startswith(prefix) or not consumption_statistic.endswith(
        _ENERGY_CONSUMPTION_SUFFIX
    ):
        return None
    return (
        consumption_statistic[: -len(_ENERGY_CONSUMPTION_SUFFIX)]
        + _ENERGY_COST_SUFFIX
    )


def _bind_owned_cost(source: dict) -> bool:
    """Make this integration's external total-cost statistic authoritative.

    Home Assistant does not support entity/fixed-price costing for an external
    consumption statistic. Older Energy preferences can nevertheless retain one of
    those stale fields after the consumption source is migrated. When the source is
    one of our external consumption statistics and no explicit cost statistic has
    been chosen, clear those stale price fields and bind the matching external cost
    statistic instead.
    """
    cost_statistic = _matching_cost_statistic(source.get("stat_energy_from"))
    if cost_statistic is None or source.get("stat_cost") is not None:
        return False

    changed = False
    if source.get("entity_energy_price") is not None:
        source["entity_energy_price"] = None
        changed = True
    if source.get("number_energy_price") is not None:
        source["number_energy_price"] = None
        changed = True

    source["stat_cost"] = cost_statistic
    return True or changed


async def async_migrate_energy_consumption_statistics(
    hass: HomeAssistant,
    legacy_to_external: dict[str, str],
) -> bool:
    """Migrate Hildebrand Energy references and bind the matching cost statistic.

    Earlier releases used the live sensor's Recorder statistic for both imported
    history and current-day data. 2.3.4 gives the integration a single external
    statistic owner instead. Existing Energy-dashboard configuration is migrated
    in place so users do not have to remove/re-add their grid or gas source.

    From 2.3.6, an already-external Hildebrand source is also repaired when Home
    Assistant is still carrying a stale fixed/entity price (for example an old gas
    rate on the electricity source). The matching integration-owned total-cost
    statistic becomes authoritative without touching an explicitly selected
    alternative cost statistic.
    """
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
        if _bind_owned_cost(source):
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
                if _bind_owned_cost(flow):
                    changed = True

    if changed:
        await manager.async_update({"energy_sources": sources})
    return changed
