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


def test_cost_components_are_single_owner_stackable_monetary_sensors() -> None:
    for key in (
        "electricity_usage_cost",
        "electricity_standing_charge",
        "gas_usage_cost",
        "gas_standing_charge",
    ):
        description = SENSOR_DESCRIPTIONS[key]
        assert description["device_class"] == SensorDeviceClass.MONETARY
        # The integration imports both historical and current-day component stats.
        # Recorder must not independently compile a second series from live states.
        assert "state_class" not in description
        assert description["native_unit_of_measurement"] == "GBP"
        assert description["data_key"] == "costs"
        assert description["daily_reset"] is True


def test_daily_cost_sensors_expose_reset_boundaries_for_recorder() -> None:
    for key in ("electricity_daily_cost", "gas_daily_cost"):
        description = SENSOR_DESCRIPTIONS[key]
        assert description["device_class"] == SensorDeviceClass.MONETARY
        assert description["state_class"] == SensorStateClass.TOTAL
        assert description["daily_reset"] is True
        assert description["diagnostic_commodity"] in ("electricity", "gas")


def test_consumption_sensors_show_today_only_and_are_not_statistics_owner() -> None:
    for classifier in (
        CLASSIFIER_ELECTRICITY_CONSUMPTION,
        CLASSIFIER_GAS_CONSUMPTION,
    ):
        description = SENSOR_DESCRIPTIONS[classifier]
        assert description["device_class"] == SensorDeviceClass.ENERGY
        assert "state_class" not in description
        assert description["data_key"] == "readings"
        assert description["name"].endswith("Today")
