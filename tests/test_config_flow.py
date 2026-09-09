from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hildebrand_glow.config_flow import (
    HildebrandGlowConfigFlow,
    HildebrandGlowOptionsFlow,
)
from custom_components.hildebrand_glow.const import (
    CONF_ELECTRICITY_RATE,
    CONF_ELECTRICITY_STANDING_CHARGE,
    CONF_GAS_RATE,
    CONF_GAS_STANDING_CHARGE,
    CONF_VIRTUAL_ENTITY,
    DOMAIN,
)


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url: str) -> None:
    """Prepare Recorder database metadata before the hass fixture starts."""
    # The custom-component pytest plugin requires recorder_db_url to be resolved
    # before hass marks itself as initialized. The hass fixture depends on this
    # hook specifically so Recorder-backed tests can establish that ordering.
    assert recorder_db_url


def test_options_flow_factory_does_not_assign_read_only_config_entry() -> None:
    flow = HildebrandGlowConfigFlow.async_get_options_flow(object())

    assert isinstance(flow, HildebrandGlowOptionsFlow)


@pytest.mark.asyncio
async def test_options_flow_exposes_reset_history_action(recorder_mock, hass) -> None:
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
async def test_settings_default_to_latest_retrieved_tariff(recorder_mock, hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_VIRTUAL_ENTITY: "site-123",
            CONF_ELECTRICITY_RATE: 0.111,
            CONF_ELECTRICITY_STANDING_CHARGE: 0.222,
            CONF_GAS_RATE: 0.033,
            CONF_GAS_STANDING_CHARGE: 0.044,
        },
    )
    entry.add_to_hass(hass)
    retrieved = {
        CONF_ELECTRICITY_RATE: 0.245,
        CONF_ELECTRICITY_STANDING_CHARGE: 0.582,
        CONF_GAS_RATE: 0.065,
        CONF_GAS_STANDING_CHARGE: 0.31,
    }

    with patch(
        "custom_components.hildebrand_glow.config_flow.async_latest_tariff_defaults",
        new=AsyncMock(return_value=retrieved),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"next_step_id": "settings"},
        )

    assert result["type"] is FlowResultType.FORM
    defaults = {
        marker.schema: marker.default()
        for marker in result["data_schema"].schema
        if marker.schema in retrieved
    }
    assert defaults == retrieved


@pytest.mark.asyncio
async def test_settings_preserve_explicit_tariff_override(recorder_mock, hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_VIRTUAL_ENTITY: "site-123"},
        options={CONF_ELECTRICITY_RATE: 0.199},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.hildebrand_glow.config_flow.async_latest_tariff_defaults",
        new=AsyncMock(return_value={CONF_ELECTRICITY_RATE: 0.245}),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"next_step_id": "settings"},
        )

    marker = next(
        item
        for item in result["data_schema"].schema
        if item.schema == CONF_ELECTRICITY_RATE
    )
    assert marker.default() == 0.199


@pytest.mark.asyncio
async def test_reset_history_requires_confirmation_and_runs_reset(
    recorder_mock,
    hass,
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


@pytest.mark.asyncio
async def test_loaded_reset_unloads_resets_and_reloads_in_order(recorder_mock, hass) -> None:
    """The maintenance action must bring an already-loaded config entry back up."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_VIRTUAL_ENTITY: "site-123"},
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    calls: list[str] = []

    async def unload(_entry_id: str) -> bool:
        calls.append("unload")
        return True

    async def reset(_hass, _entry) -> list[str]:
        calls.append("reset")
        return ["hildebrand_glow:cost"]

    async def setup(_entry_id: str) -> bool:
        calls.append("setup")
        return True

    with (
        patch.object(hass.config_entries, "async_unload", side_effect=unload),
        patch(
            "custom_components.hildebrand_glow.config_flow.async_reset_imported_history",
            side_effect=reset,
        ),
        patch.object(hass.config_entries, "async_setup", side_effect=setup),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"next_step_id": "reset_history"},
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reset_complete"
    assert calls == ["unload", "reset", "setup"]


@pytest.mark.asyncio
async def test_loaded_reset_does_not_clear_history_when_unload_fails(
    recorder_mock,
    hass,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_VIRTUAL_ENTITY: "site-123"},
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)

    with (
        patch.object(
            hass.config_entries,
            "async_unload",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "custom_components.hildebrand_glow.config_flow.async_reset_imported_history",
            new=AsyncMock(),
        ) as reset,
        patch.object(hass.config_entries, "async_setup", new=AsyncMock()) as setup,
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"next_step_id": "reset_history"},
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "reset_failed"}
    reset.assert_not_awaited()
    setup.assert_not_awaited()
