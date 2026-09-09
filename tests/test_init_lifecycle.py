from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hildebrand_glow import (
    _async_cleanup_legacy_statistics_after_setup,
    _tariff_config,
    async_unload_entry,
    async_update_options,
)
from custom_components.hildebrand_glow.const import (
    CONF_CONSUMPTION_INTERVAL,
    CONF_COST_INTERVAL,
    CONF_ELECTRICITY_RATE,
    CONF_ELECTRICITY_STANDING_CHARGE,
    CONF_GAS_RATE,
    CONF_GAS_STANDING_CHARGE,
    DOMAIN,
)


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "password",
            CONF_ELECTRICITY_RATE: 0.20,
            CONF_ELECTRICITY_STANDING_CHARGE: 0.40,
            CONF_GAS_RATE: 0.06,
            CONF_GAS_STANDING_CHARGE: 0.30,
            CONF_CONSUMPTION_INTERVAL: 15,
            CONF_COST_INTERVAL: 60,
        },
        options={
            CONF_ELECTRICITY_RATE: 0.25,
            CONF_CONSUMPTION_INTERVAL: 30,
        },
    )


def test_tariff_config_prefers_options_over_entry_data() -> None:
    entry = _entry()

    assert _tariff_config(entry) == {
        "electricity_rate": 0.25,
        "gas_rate": 0.06,
        "electricity_standing_charge": 0.40,
        "gas_standing_charge": 0.30,
    }


@pytest.mark.asyncio
async def test_update_options_updates_coordinator_and_refreshes(hass) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = MagicMock()
    coordinator.async_request_refresh = AsyncMock()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await async_update_options(hass, entry)

    coordinator.update_settings.assert_called_once_with(
        {
            "electricity_rate": 0.25,
            "gas_rate": 0.06,
            "electricity_standing_charge": 0.40,
            "gas_standing_charge": 0.30,
        },
        30,
        60,
    )
    coordinator.async_request_refresh.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("unload_ok", [True, False])
async def test_unload_always_stops_coordinator_and_only_removes_data_on_success(
    hass,
    unload_ok: bool,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = MagicMock()
    coordinator.async_shutdown = AsyncMock()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new=AsyncMock(return_value=unload_ok),
    ) as unload:
        result = await async_unload_entry(hass, entry)

    assert result is unload_ok
    coordinator.async_shutdown.assert_awaited_once_with()
    unload.assert_awaited_once()
    assert (entry.entry_id in hass.data[DOMAIN]) is (not unload_ok)


@pytest.mark.asyncio
async def test_cleanup_failure_is_logged_without_escaping(hass, caplog) -> None:
    entry = _entry()
    error = RuntimeError("recorder unavailable")

    with (
        patch(
            "custom_components.hildebrand_glow.async_cleanup_legacy_statistics",
            new=AsyncMock(side_effect=error),
        ),
        caplog.at_level(logging.ERROR),
    ):
        await _async_cleanup_legacy_statistics_after_setup(hass, entry)

    assert "Failed to clean up legacy Hildebrand statistics" in caplog.text


@pytest.mark.asyncio
async def test_cleanup_cancellation_propagates(hass) -> None:
    entry = _entry()

    with patch(
        "custom_components.hildebrand_glow.async_cleanup_legacy_statistics",
        new=AsyncMock(side_effect=asyncio.CancelledError),
    ):
        with pytest.raises(asyncio.CancelledError):
            await _async_cleanup_legacy_statistics_after_setup(hass, entry)
