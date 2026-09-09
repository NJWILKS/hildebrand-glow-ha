from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hildebrand_glow.const import CONF_VIRTUAL_ENTITY, DOMAIN


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url: str) -> None:
    assert recorder_db_url


async def _open_reset_flow(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"next_step_id": "reset_history"},
    )
    assert result["type"] is FlowResultType.FORM
    return result


@pytest.mark.asyncio
async def test_reset_exception_reports_failure_and_reloads_loaded_entry(recorder_mock, hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_VIRTUAL_ENTITY: "site-123"},
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)

    with (
        patch.object(hass.config_entries, "async_unload", new=AsyncMock(return_value=True)),
        patch(
            "custom_components.hildebrand_glow.config_flow.async_reset_imported_history",
            new=AsyncMock(side_effect=RuntimeError("recorder failed")),
        ),
        patch.object(hass.config_entries, "async_setup", new=AsyncMock(return_value=True)) as setup,
    ):
        result = await _open_reset_flow(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "reset_failed"}
    setup.assert_awaited_once_with(entry.entry_id)


@pytest.mark.asyncio
async def test_successful_reset_reports_reload_failure_when_setup_returns_false(recorder_mock, hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_VIRTUAL_ENTITY: "site-123"},
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)

    with (
        patch.object(hass.config_entries, "async_unload", new=AsyncMock(return_value=True)),
        patch(
            "custom_components.hildebrand_glow.config_flow.async_reset_imported_history",
            new=AsyncMock(return_value=["hildebrand_glow:cost"]),
        ),
        patch.object(hass.config_entries, "async_setup", new=AsyncMock(return_value=False)),
    ):
        result = await _open_reset_flow(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "reload_failed"}
