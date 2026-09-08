"""Resolve Home Assistant tariff-setting defaults from the stored Glow ledger."""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_ELECTRICITY_RATE,
    CONF_ELECTRICITY_STANDING_CHARGE,
    CONF_GAS_RATE,
    CONF_GAS_STANDING_CHARGE,
    DOMAIN,
)
from .cost_ingestion import TARIFF_HISTORY_STORAGE_VERSION
from .tariff import parse_tariff_periods

_RATE_KEYS = {
    "electricity": (CONF_ELECTRICITY_RATE, CONF_ELECTRICITY_STANDING_CHARGE),
    "gas": (CONF_GAS_RATE, CONF_GAS_STANDING_CHARGE),
}


def latest_tariff_defaults(ledger: dict[str, Any]) -> dict[str, float]:
    """Return current flat-rate/standing-charge values in pounds from a tariff ledger."""
    result: dict[str, float] = {}
    commodities = ledger.get("commodities", {})
    if not isinstance(commodities, dict):
        return result

    for commodity, (rate_key, standing_key) in _RATE_KEYS.items():
        rows = commodities.get(commodity, [])
        if not isinstance(rows, list):
            continue
        periods = parse_tariff_periods([row for row in rows if isinstance(row, dict)])
        if not periods:
            continue
        latest = periods[-1]
        if latest.unit_rate_pence_per_kwh is not None:
            result[rate_key] = round(latest.unit_rate_pence_per_kwh / 100.0, 6)
        if latest.standing_pence is not None:
            result[standing_key] = round(latest.standing_pence / 100.0, 6)

    return result


async def async_latest_tariff_defaults(
    hass: HomeAssistant,
    site_id: str,
) -> dict[str, float]:
    """Load the most recently persisted Glow tariff values for a meter site."""
    store = Store(
        hass,
        TARIFF_HISTORY_STORAGE_VERSION,
        f"{DOMAIN}_{site_id}_tariff_history",
    )
    ledger = await store.async_load() or {}
    return latest_tariff_defaults(ledger)
