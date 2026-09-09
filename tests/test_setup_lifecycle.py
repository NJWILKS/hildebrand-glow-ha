from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hildebrand_glow import async_setup_entry
from custom_components.hildebrand_glow.const import CONF_VIRTUAL_ENTITY, DOMAIN


@pytest.mark.asyncio
async def test_config_entry_setup_does_not_wait_for_legacy_statistics_cleanup(hass) -> None:
    """Recorder cleanup may be slow, but it must never hold config-entry setup open."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="DCC Sourced",
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "password",
            CONF_VIRTUAL_ENTITY: "site-123",
        },
    )
    entry.add_to_hass(hass)

    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def slow_cleanup(*_args) -> list[str]:
        cleanup_started.set()
        await cleanup_release.wait()
        return []

    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    coordinator.schedule_history_backfill = MagicMock()
    coordinator.async_shutdown = AsyncMock()

    with (
        patch(
            "custom_components.hildebrand_glow.GlowmarktDataUpdateCoordinator",
            return_value=coordinator,
        ),
        patch(
            "custom_components.hildebrand_glow.async_cleanup_legacy_statistics",
            side_effect=slow_cleanup,
        ),
        patch(
            "custom_components.hildebrand_glow.async_migrate_energy_consumption_statistics",
            new=AsyncMock(return_value=False),
        ) as migrate,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(),
        ) as forward,
    ):
        setup_ok = await asyncio.wait_for(async_setup_entry(hass, entry), timeout=1)
        assert setup_ok is True
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)

        forward.assert_awaited_once_with(entry, ["sensor"])
        migrate.assert_awaited_once_with(hass, {})
        coordinator.schedule_history_backfill.assert_called_once_with()

        cleanup_release.set()
        await asyncio.sleep(0)
