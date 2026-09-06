from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from custom_components.hildebrand_glow.sensor import SENSOR_DESCRIPTIONS


def test_daily_standing_charge_uses_valid_monetary_state_class() -> None:
    description = SENSOR_DESCRIPTIONS["daily_standing_charges"]

    assert description["device_class"] == SensorDeviceClass.MONETARY
    assert description["state_class"] == SensorStateClass.TOTAL
