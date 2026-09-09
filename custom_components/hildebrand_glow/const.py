"""Constants for the Hildebrand Glow integration."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "hildebrand_glow"
GLOWMARKT_API_BASE: Final = "https://api.glowmarkt.com/api/v0-1"
GLOWMARKT_APP_ID: Final = "b0f1b774-a586-4f72-9edd-27ead8aa7a8d"

CONF_VIRTUAL_ENTITY: Final = "virtual_entity_id"
CONF_CONSUMPTION_INTERVAL: Final = "consumption_interval_minutes"
CONF_COST_INTERVAL: Final = "api_cost_interval_minutes"

DEFAULT_CONSUMPTION_INTERVAL: Final = 15
DEFAULT_COST_INTERVAL: Final = 60
MIN_POLL_INTERVAL: Final = 5
HISTORY_START_DELAY_SECONDS: Final = 15

DEFAULT_ELECTRICITY_RATE: Final = 0.245
DEFAULT_GAS_RATE: Final = 0.065
DEFAULT_ELECTRICITY_STANDING_CHARGE: Final = 0.45
DEFAULT_GAS_STANDING_CHARGE: Final = 0.30
CONF_ELECTRICITY_RATE: Final = "electricity_rate"
CONF_GAS_RATE: Final = "gas_rate"
CONF_ELECTRICITY_STANDING_CHARGE: Final = "electricity_standing_charge"
CONF_GAS_STANDING_CHARGE: Final = "gas_standing_charge"

CLASSIFIER_ELECTRICITY_CONSUMPTION: Final = "electricity.consumption"
CLASSIFIER_ELECTRICITY_COST: Final = "electricity.consumption.cost"
CLASSIFIER_GAS_CONSUMPTION: Final = "gas.consumption"
CLASSIFIER_GAS_COST: Final = "gas.consumption.cost"

ATTRIBUTION: Final = "Data provided by Hildebrand Technology via Glowmarkt API"
