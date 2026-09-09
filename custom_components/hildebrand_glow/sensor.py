"""Sensor platform for Hildebrand Glow integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTRIBUTION,
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_ELECTRICITY_COST,
    CLASSIFIER_GAS_CONSUMPTION,
    CLASSIFIER_GAS_COST,
    CONF_VIRTUAL_ENTITY,
    DOMAIN,
)
from .coordinator import GlowmarktDataUpdateCoordinator
from .cost_ingestion import async_cost_ingestion_worker
from .identity import sensor_unique_id, site_identity

# All visible sensors are presentation/diagnostic surfaces only. Long-term Energy,
# total-cost and cost-component history is owned exclusively by integration-owned
# external statistics. Deliberately omitting state_class from every entity prevents
# Recorder from creating a second set of long-term statistics.
SENSOR_DESCRIPTIONS: dict[str, dict[str, Any]] = {
    CLASSIFIER_ELECTRICITY_CONSUMPTION: {
        "name": "Electricity Consumption Today",
        "icon": "mdi:flash",
        "device_class": SensorDeviceClass.ENERGY,
        "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        "data_key": "readings",
        "reading_key": CLASSIFIER_ELECTRICITY_CONSUMPTION,
    },
    CLASSIFIER_GAS_CONSUMPTION: {
        "name": "Gas Consumption Today",
        "icon": "mdi:fire",
        "device_class": SensorDeviceClass.ENERGY,
        "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        "data_key": "readings",
        "reading_key": CLASSIFIER_GAS_CONSUMPTION,
    },
    f"{CLASSIFIER_ELECTRICITY_COST}_api": {
        "name": "Electricity Cost (API)",
        "icon": "mdi:currency-gbp",
        "device_class": SensorDeviceClass.MONETARY,
        "native_unit_of_measurement": "GBP",
        "data_key": "readings",
        "reading_key": CLASSIFIER_ELECTRICITY_COST,
        "convert_pence": True,
        "diagnostic_commodity": "electricity",
    },
    f"{CLASSIFIER_GAS_COST}_api": {
        "name": "Gas Cost (API)",
        "icon": "mdi:currency-gbp",
        "device_class": SensorDeviceClass.MONETARY,
        "native_unit_of_measurement": "GBP",
        "data_key": "readings",
        "reading_key": CLASSIFIER_GAS_COST,
        "convert_pence": True,
        "diagnostic_commodity": "gas",
    },
    "electricity_daily_cost": {
        "name": "Electricity Daily Cost",
        "icon": "mdi:currency-gbp",
        "device_class": SensorDeviceClass.MONETARY,
        "native_unit_of_measurement": "GBP",
        "data_key": "costs",
        "reading_key": "electricity",
        "diagnostic_commodity": "electricity",
    },
    "electricity_usage_cost": {
        "name": "Electricity Usage Cost",
        "icon": "mdi:flash-outline",
        "device_class": SensorDeviceClass.MONETARY,
        "native_unit_of_measurement": "GBP",
        "data_key": "costs",
        "reading_key": "electricity_usage",
        "diagnostic_commodity": "electricity",
    },
    "electricity_standing_charge": {
        "name": "Electricity Standing Charge",
        "icon": "mdi:cash-clock",
        "device_class": SensorDeviceClass.MONETARY,
        "native_unit_of_measurement": "GBP",
        "data_key": "costs",
        "reading_key": "electricity_standing_charge",
        "diagnostic_commodity": "electricity",
    },
    "gas_daily_cost": {
        "name": "Gas Daily Cost",
        "icon": "mdi:currency-gbp",
        "device_class": SensorDeviceClass.MONETARY,
        "native_unit_of_measurement": "GBP",
        "data_key": "costs",
        "reading_key": "gas",
        "diagnostic_commodity": "gas",
    },
    "gas_usage_cost": {
        "name": "Gas Usage Cost",
        "icon": "mdi:fire",
        "device_class": SensorDeviceClass.MONETARY,
        "native_unit_of_measurement": "GBP",
        "data_key": "costs",
        "reading_key": "gas_usage",
        "diagnostic_commodity": "gas",
    },
    "gas_standing_charge": {
        "name": "Gas Standing Charge",
        "icon": "mdi:cash-clock",
        "device_class": SensorDeviceClass.MONETARY,
        "native_unit_of_measurement": "GBP",
        "data_key": "costs",
        "reading_key": "gas_standing_charge",
        "diagnostic_commodity": "gas",
    },
    "total_daily_cost": {
        "name": "Total Daily Energy Cost",
        "icon": "mdi:currency-gbp",
        "device_class": SensorDeviceClass.MONETARY,
        "native_unit_of_measurement": "GBP",
        "data_key": "costs",
        "reading_key": "total",
    },
    "daily_standing_charges": {
        "name": "Daily Standing Charges",
        "icon": "mdi:cash-clock",
        "device_class": SensorDeviceClass.MONETARY,
        "native_unit_of_measurement": "GBP",
        "data_key": "costs",
        "reading_key": "standing_charges_total",
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create entities and own the single cost-ingestion worker for this entry."""
    coordinator: GlowmarktDataUpdateCoordinator = hass.data[DOMAIN][
        config_entry.entry_id
    ]
    site_id = site_identity(
        config_entry.data.get(CONF_VIRTUAL_ENTITY),
        config_entry.entry_id,
    )

    entities = [
        GlowmarktSensor(
            coordinator=coordinator,
            sensor_key=sensor_key,
            description=description,
            site_id=site_id,
        )
        for sensor_key, description in SENSOR_DESCRIPTIONS.items()
    ]
    async_add_entities(entities)

    if any(
        classifier in coordinator.resources
        for classifier in (CLASSIFIER_ELECTRICITY_COST, CLASSIFIER_GAS_COST)
    ):
        config_entry.async_create_background_task(
            hass,
            async_cost_ingestion_worker(hass, coordinator, site_id),
            f"{DOMAIN} cost ingestion worker",
        )


class GlowmarktSensor(
    CoordinatorEntity[GlowmarktDataUpdateCoordinator],
    SensorEntity,
):
    """One Hildebrand Glow sensor."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GlowmarktDataUpdateCoordinator,
        sensor_key: str,
        description: dict[str, Any],
        site_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._sensor_key = sensor_key
        self._description = description

        reading_key = description.get("reading_key")
        resource = (
            coordinator.resources.get(reading_key)
            if isinstance(reading_key, str)
            else None
        )
        resource_id = resource.get("resource_id") if resource else None

        self._attr_unique_id = sensor_unique_id(
            site_id,
            sensor_key,
            resource_id,
        )
        self._attr_name = description["name"]
        self._attr_icon = description.get("icon")
        self._attr_device_class = description.get("device_class")
        self._attr_native_unit_of_measurement = description.get(
            "native_unit_of_measurement"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, site_id)},
            name="Smart Meter",
            manufacturer="Hildebrand Technology",
            model="SMETS2 via Glow/Bright",
            configuration_url="https://glowmarkt.com/",
        )

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        data_key = self._description.get("data_key", "readings")
        reading_key = self._description.get("reading_key", "")
        data_section = self.coordinator.data.get(data_key, {})
        value = data_section.get(reading_key)

        if value is None and reading_key in ("electricity_usage", "gas_usage"):
            commodity = reading_key.removesuffix("_usage")
            value = (
                self.coordinator.data.get("cost_diagnostics", {})
                .get(commodity, {})
                .get("usage_cost_gbp")
            )

        if value is None:
            return None
        if self._description.get("convert_pence", False):
            value = round(value / 100.0, 2)
        if isinstance(value, float):
            if self._attr_device_class == SensorDeviceClass.MONETARY:
                return round(value, 2)
            return round(value, 3)
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        commodity = self._description.get("diagnostic_commodity")
        if commodity is None or self.coordinator.data is None:
            return None
        diagnostics = self.coordinator.data.get("cost_diagnostics", {}).get(commodity)
        if not diagnostics:
            return None
        return dict(diagnostics)
