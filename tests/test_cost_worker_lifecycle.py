from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

import custom_components.hildebrand_glow as integration
import custom_components.hildebrand_glow.cost_ingestion as cost_ingestion
from custom_components.hildebrand_glow.const import (
    CONF_VIRTUAL_ENTITY,
    DOMAIN,
)


class _Entry:
    def __init__(self) -> None:
        self.entry_id = "entry-1"
        self.data = {
            CONF_USERNAME: "user",
            CONF_PASSWORD: "password",
            CONF_VIRTUAL_ENTITY: "site-123",
        }
        self.options: dict[str, object] = {}
        self.unload_callbacks: list[object] = []

    def async_on_unload(self, callback) -> None:
        self.unload_callbacks.append(callback)

    def add_update_listener(self, _listener):
        return MagicMock(name="remove_update_listener")


@pytest.mark.asyncio
async def test_setup_starts_cost_worker_only_after_sensor_setup() -> None:
    """Regression: cost history must be primed after entities are registered."""
    events: list[str] = []
    entry = _Entry()
    task = MagicMock(name="cost_ingestion_task")

    coordinator = MagicMock(name="coordinator")
    coordinator.async_config_entry_first_refresh = AsyncMock(
        side_effect=lambda: events.append("first_refresh")
    )
    coordinator.schedule_history_backfill.side_effect = lambda: events.append(
        "history_backfill_scheduled"
    )

    async def _forward_entry_setups(_entry, _platforms) -> None:
        events.append("sensor_setup_complete")

    def _create_background_task(_coro, *, name: str):
        assert name == f"{DOMAIN} cost ingestion"
        events.append("cost_worker_started")
        return task

    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(side_effect=_forward_entry_setups)
        ),
        async_create_background_task=MagicMock(side_effect=_create_background_task),
    )

    with (
        patch.object(integration, "async_get_clientsession", return_value=object()),
        patch.object(integration, "GlowmarktApiClient", return_value=object()),
        patch.object(
            integration,
            "GlowmarktDataUpdateCoordinator",
            return_value=coordinator,
        ),
        patch.object(
            integration,
            "async_cost_ingestion_worker",
            return_value=object(),
        ) as worker,
    ):
        assert await integration.async_setup_entry(hass, entry) is True

    assert events == [
        "first_refresh",
        "sensor_setup_complete",
        "history_backfill_scheduled",
        "cost_worker_started",
    ]
    worker.assert_called_once_with(hass, coordinator, "site-123")
    assert task.cancel in entry.unload_callbacks


@pytest.mark.asyncio
async def test_cost_worker_fetches_tariff_ledger_before_first_reconcile() -> None:
    """Regression: historical costs are always rebuilt from tariff history first."""
    events: list[str] = []
    ledger = {
        "commodities": {
            "electricity": [
                {
                    "effectiveDate": "2026-04-01 00:00:00",
                    "plan": [
                        {"planDetail": [{"rate": 23.1}, {"standing": 55.2}]}
                    ],
                },
                {
                    "effectiveDate": "2026-07-01 00:00:00",
                    "plan": [
                        {"planDetail": [{"rate": 24.5}, {"standing": 58.2}]}
                    ],
                },
            ]
        }
    }

    async def _refresh(_hass, _coordinator, _site_id):
        events.append("tariff_history")
        return ledger

    async def _sync(_hass, _coordinator, _site_id, *, tariff_ledger=None):
        assert tariff_ledger is ledger
        events.append("cost_reconcile")
        raise asyncio.CancelledError

    with (
        patch.object(cost_ingestion.asyncio, "sleep", new=AsyncMock()),
        patch.object(cost_ingestion, "_refresh_tariff_ledger", side_effect=_refresh),
        patch.object(cost_ingestion, "async_sync_cost_ingestion", side_effect=_sync),
    ):
        with pytest.raises(asyncio.CancelledError):
            await cost_ingestion.async_cost_ingestion_worker(
                object(),
                object(),
                "site-123",
            )

    assert events == ["tariff_history", "cost_reconcile"]
