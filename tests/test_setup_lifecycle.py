from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hildebrand_glow import async_setup_entry
from custom_components.hildebrand_glow.const import CONF_VIRTUAL_ENTITY, DOMAIN


@pytest.mark.asyncio
async def test_config_entry_setup_starts_only_interval_history_worker(hass) -> None:
    """2.5 setup must not start any legacy Recorder history machinery."""
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
    entry.async_create_background_task = MagicMock()

    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    coordinator.async_shutdown = AsyncMock()
    coordinator.resources = {}

    async def worker_coro() -> None:
        return None

    task = worker_coro()
    worker = MagicMock(return_value=task)

    with (
        patch(
            "custom_components.hildebrand_glow.LedgerOnlyGlowmarktDataUpdateCoordinator",
            return_value=coordinator,
        ),
        patch(
            "custom_components.hildebrand_glow.async_interval_history_worker",
            new=worker,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(),
        ) as forward,
    ):
        setup_ok = await async_setup_entry(hass, entry)

    assert setup_ok is True
    forward.assert_awaited_once_with(entry, ["sensor"])
    worker.assert_called_once()
    entry.async_create_background_task.assert_called_once_with(
        hass,
        task,
        f"{DOMAIN} PT30M interval history population",
    )
    coordinator.schedule_history_backfill.assert_not_called()
    task.close()
