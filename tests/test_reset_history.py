from __future__ import annotations

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hildebrand_glow.const import CONF_VIRTUAL_ENTITY, DOMAIN
from custom_components.hildebrand_glow.consumption_statistics import (
    energy_consumption_statistic_id,
)
from custom_components.hildebrand_glow.cost_ingestion import (
    cost_component_statistic_id,
    energy_cost_statistic_id,
)
from custom_components.hildebrand_glow.reset import (
    legacy_cleanup_statistic_ids,
    reset_statistic_ids,
)


def _entry_and_registry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_VIRTUAL_ENTITY: "site-123"},
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)

    sensor = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "site-123-electricity",
        config_entry=entry,
        suggested_object_id="smart_meter_electricity_consumption",
    )
    button = registry.async_get_or_create(
        "button",
        DOMAIN,
        "site-123-maintenance",
        config_entry=entry,
        suggested_object_id="maintenance",
    )
    return entry, registry, sensor, button


def test_reset_statistic_ids_are_scoped_to_entry_and_all_owned_statistics(hass) -> None:
    entry, registry, sensor, button = _entry_and_registry(hass)

    statistic_ids = reset_statistic_ids(registry, entry, "site-123")

    assert sensor.entity_id in statistic_ids
    assert button.entity_id not in statistic_ids
    assert energy_consumption_statistic_id("site-123", "electricity") in statistic_ids
    assert energy_consumption_statistic_id("site-123", "gas") in statistic_ids
    assert energy_cost_statistic_id("site-123", "electricity") in statistic_ids
    assert energy_cost_statistic_id("site-123", "gas") in statistic_ids
    assert (
        cost_component_statistic_id("site-123", "electricity", "usage_cost")
        in statistic_ids
    )
    assert (
        cost_component_statistic_id("site-123", "electricity", "standing_charge")
        in statistic_ids
    )
    assert cost_component_statistic_id("site-123", "gas", "usage_cost") in statistic_ids
    assert (
        cost_component_statistic_id("site-123", "gas", "standing_charge")
        in statistic_ids
    )


def test_upgrade_cleanup_preserves_external_consumption_but_removes_old_cost_stats(hass) -> None:
    entry, registry, sensor, button = _entry_and_registry(hass)

    statistic_ids = legacy_cleanup_statistic_ids(registry, entry, "site-123")

    assert sensor.entity_id in statistic_ids
    assert button.entity_id not in statistic_ids
    assert energy_consumption_statistic_id("site-123", "electricity") not in statistic_ids
    assert energy_consumption_statistic_id("site-123", "gas") not in statistic_ids
    assert energy_cost_statistic_id("site-123", "electricity") in statistic_ids
    assert energy_cost_statistic_id("site-123", "gas") in statistic_ids
    assert (
        cost_component_statistic_id("site-123", "electricity", "usage_cost")
        in statistic_ids
    )
    assert (
        cost_component_statistic_id("site-123", "electricity", "standing_charge")
        in statistic_ids
    )
