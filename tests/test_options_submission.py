from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hildebrand_glow.const import (
    CONF_CONSUMPTION_INTERVAL,
    CONF_COST_INTERVAL,
    CONF_ELECTRICITY_RATE,
    CONF_ELECTRICITY_STANDING_CHARGE,
    CONF_GAS_RATE,
    CONF_GAS_STANDING_CHARGE,
    CONF_VIRTUAL_ENTITY,
    DOMAIN,
)


@pytest.mark.asyncio
async def test_settings_submission_changes_options_without_mutating_identity_data(
    hass,
) -> None:
    original_data = {
        CONF_USERNAME: "user@example.com",
        CONF_PASSWORD: "secret",
        CONF_VIRTUAL_ENTITY: "site-123",
        CONF_ELECTRICITY_RATE: 0.20,
        CONF_ELECTRICITY_STANDING_CHARGE: 0.40,
        CONF_GAS_RATE: 0.06,
        CONF_GAS_STANDING_CHARGE: 0.30,
        CONF_CONSUMPTION_INTERVAL: 30,
        CONF_COST_INTERVAL: 60,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=original_data)
    entry.add_to_hass(hass)
    new_options = {
        CONF_ELECTRICITY_RATE: 0.25,
        CONF_ELECTRICITY_STANDING_CHARGE: 0.50,
        CONF_GAS_RATE: 0.07,
        CONF_GAS_STANDING_CHARGE: 0.35,
        CONF_CONSUMPTION_INTERVAL: 15,
        CONF_COST_INTERVAL: 30,
    }

    with patch(
        "custom_components.hildebrand_glow.config_flow.async_latest_tariff_defaults",
        new=AsyncMock(return_value={}),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"next_step_id": "settings"},
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input=new_options,
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data == original_data
    assert entry.options == new_options
