from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hildebrand_glow.config_flow import (
    HildebrandGlowConfigFlow,
    HildebrandGlowOptionsFlow,
)
from custom_components.hildebrand_glow.const import CONF_VIRTUAL_ENTITY, DOMAIN


def test_options_flow_factory_does_not_assign_read_only_config_entry() -> None:
    flow = HildebrandGlowConfigFlow.async_get_options_flow(object())

    assert isinstance(flow, HildebrandGlowOptionsFlow)


@pytest.mark.asyncio
async def test_options_flow_exposes_reset_history_action(hass, recorder_mock) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_VIRTUAL_ENTITY: "site-123"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == ["settings", "reset_history"]


@pytest.mark.asyncio
async def test_reset_history_requires_confirmation_and_runs_reset(
    hass,
    recorder_mock,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_VIRTUAL_ENTITY: "site-123"},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.hildebrand_glow.config_flow.async_reset_imported_history",
        new=AsyncMock(return_value=["sensor.one", "hildebrand_glow:cost"]),
    ) as reset:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"next_step_id": "reset_history"},
        )

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reset_history"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reset_complete"
    assert result["description_placeholders"] == {"count": "2"}
    reset.assert_awaited_once_with(hass, entry)
