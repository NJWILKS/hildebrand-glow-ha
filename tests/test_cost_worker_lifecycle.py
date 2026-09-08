from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.hildebrand_glow.cost_ingestion as cost_ingestion
import custom_components.hildebrand_glow.sensor as sensor_platform
from custom_components.hildebrand_glow.const import (
    CLASSIFIER_ELECTRICITY_COST,
    CONF_VIRTUAL_ENTITY,
    DOMAIN,
)


@pytest.mark.asyncio
async def test_sensor_platform_owns_exactly_one_cost_worker() -> None:
    """Regression: setup must not create duplicate cost-ingestion workers."""
    coordinator = SimpleNamespace(
        resources={CLASSIFIER_ELECTRICITY_COST: {"resource_id": "cost-resource"}}
    )
    hass = SimpleNamespace(data={DOMAIN: {"entry-1": coordinator}})
    created: list[tuple[object, str]] = []
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={CONF_VIRTUAL_ENTITY: "site-123"},
        async_create_background_task=lambda _hass, target, name: created.append(
            (target, name)
        ),
    )
    add_entities = MagicMock()
    worker_token = object()

    with (
        patch.object(sensor_platform, "GlowmarktSensor", return_value=MagicMock()),
        patch.object(
            sensor_platform,
            "async_cost_ingestion_worker",
            return_value=worker_token,
        ) as worker,
    ):
        await sensor_platform.async_setup_entry(hass, entry, add_entities)

    worker.assert_called_once_with(hass, coordinator, "site-123")
    assert created == [(worker_token, f"{DOMAIN} cost ingestion worker")]


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
