from homeassistant.components.sensor import SensorDeviceClass

from custom_components.hildebrand_glow.const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_GAS_CONSUMPTION,
)
from custom_components.hildebrand_glow.sensor import SENSOR_DESCRIPTIONS


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
