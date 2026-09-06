from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from custom_components.hildebrand_glow.const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_GAS_CONSUMPTION,
)
from custom_components.hildebrand_glow.sensor import SENSOR_DESCRIPTIONS


def test_daily_standing_charge_uses_valid_monetary_state_class() -> None:
    description = SENSOR_DESCRIPTIONS["daily_standing_charges"]

    assert description["device_class"] == SensorDeviceClass.MONETARY
    assert description["state_class"] == SensorStateClass.TOTAL


def test_consumption_sensors_read_from_persisted_cumulative_values() -> None:
    for classifier in (
        CLASSIFIER_ELECTRICITY_CONSUMPTION,
        CLASSIFIER_GAS_CONSUMPTION,
    ):
        description = SENSOR_DESCRIPTIONS[classifier]
        assert description["state_class"] == SensorStateClass.TOTAL_INCREASING
        assert description["data_key"] == "cumulative_readings"
