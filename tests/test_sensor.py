from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.sensor import SensorDeviceClass

from custom_components.hildebrand_glow.const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_ELECTRICITY_COST,
    CLASSIFIER_GAS_CONSUMPTION,
    CLASSIFIER_GAS_COST,
    DOMAIN,
)
from custom_components.hildebrand_glow.sensor import (
    SENSOR_DESCRIPTIONS,
    GlowmarktSensor,
    async_setup_entry,
)


def _coordinator(data: dict | None = None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "electricity"},
        CLASSIFIER_ELECTRICITY_COST: {"resource_id": "electricity-cost"},
        CLASSIFIER_GAS_CONSUMPTION: {"resource_id": "gas"},
        CLASSIFIER_GAS_COST: {"resource_id": "gas-cost"},
    }
    return coordinator


def _sensor(sensor_key: str, data: dict | None) -> GlowmarktSensor:
    return GlowmarktSensor(
        coordinator=_coordinator(data),
        sensor_key=sensor_key,
        description=SENSOR_DESCRIPTIONS[sensor_key],
        site_id="site-123",
    )


def test_all_visible_sensors_are_presentation_only() -> None:
    """Recorder must never become a second long-term statistics owner."""
    assert SENSOR_DESCRIPTIONS
    for description in SENSOR_DESCRIPTIONS.values():
        assert "state_class" not in description


def test_cost_components_are_stackable_monetary_presentation_sensors() -> None:
    for key in (
        "electricity_usage_cost",
        "electricity_standing_charge",
        "gas_usage_cost",
        "gas_standing_charge",
    ):
        description = SENSOR_DESCRIPTIONS[key]
        assert description["device_class"] == SensorDeviceClass.MONETARY
        assert description["native_unit_of_measurement"] == "GBP"
        assert description["data_key"] == "costs"


def test_consumption_sensors_show_today_only_and_are_not_statistics_owner() -> None:
    for classifier in (
        CLASSIFIER_ELECTRICITY_CONSUMPTION,
        CLASSIFIER_GAS_CONSUMPTION,
    ):
        description = SENSOR_DESCRIPTIONS[classifier]
        assert description["device_class"] == SensorDeviceClass.ENERGY
        assert description["data_key"] == "readings"
        assert description["name"].endswith("Today")


def test_consumption_native_value_is_contextual_and_rounded() -> None:
    sensor = _sensor(
        CLASSIFIER_ELECTRICITY_CONSUMPTION,
        {"readings": {CLASSIFIER_ELECTRICITY_CONSUMPTION: 1.23456}},
    )

    assert sensor.native_value == 1.235


def test_api_cost_sensor_converts_pence_to_gbp() -> None:
    key = f"{CLASSIFIER_ELECTRICITY_COST}_api"
    sensor = _sensor(
        key,
        {"readings": {CLASSIFIER_ELECTRICITY_COST: 123.4}},
    )

    assert sensor.native_value == 1.23


def test_usage_sensor_falls_back_to_cost_diagnostics() -> None:
    sensor = _sensor(
        "electricity_usage_cost",
        {
            "costs": {},
            "cost_diagnostics": {"electricity": {"usage_cost_gbp": 1.239}},
        },
    )

    assert sensor.native_value == 1.24


def test_sensor_returns_none_for_missing_data() -> None:
    assert _sensor("electricity_daily_cost", None).native_value is None
    assert _sensor("electricity_daily_cost", {"costs": {}}).native_value is None


def test_cost_diagnostics_are_exposed_as_attributes() -> None:
    diagnostics = {
        "source": "glow_api",
        "complete_day": False,
        "standing_charge_status": "not_applied",
    }
    sensor = _sensor(
        "electricity_daily_cost",
        {
            "costs": {"electricity": 1.25},
            "cost_diagnostics": {"electricity": diagnostics},
        },
    )

    assert sensor.extra_state_attributes == diagnostics
    assert sensor.extra_state_attributes is not diagnostics


def test_non_diagnostic_sensor_has_no_extra_attributes() -> None:
    sensor = _sensor(
        "total_daily_cost",
        {"costs": {"total": 2.0}},
    )

    assert sensor.extra_state_attributes is None


async def test_sensor_setup_adds_entities_without_history_writer(hass) -> None:
    coordinator = _coordinator({})
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry.data = {}
    entry.async_create_background_task = MagicMock()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    add_entities = MagicMock()

    await async_setup_entry(hass, entry, add_entities)

    entities = add_entities.call_args.args[0]
    assert len(entities) == len(SENSOR_DESCRIPTIONS)
    assert {entity._sensor_key for entity in entities} == set(SENSOR_DESCRIPTIONS)
    entry.async_create_background_task.assert_not_called()


async def test_unsupported_resource_sensors_are_not_created(hass) -> None:
    coordinator = _coordinator({})
    coordinator.resources = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "electricity"}
    }
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry.data = {}
    entry.async_create_background_task = MagicMock()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    add_entities = MagicMock()

    await async_setup_entry(hass, entry, add_entities)

    entities = add_entities.call_args.args[0]
    assert [entity._sensor_key for entity in entities] == [
        CLASSIFIER_ELECTRICITY_CONSUMPTION
    ]
    entry.async_create_background_task.assert_not_called()
