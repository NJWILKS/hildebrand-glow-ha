from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.hildebrand_glow.energy_migration import (
    async_migrate_energy_consumption_statistics,
)


@pytest.mark.asyncio
async def test_energy_migration_replaces_only_exact_hildebrand_consumption_reference() -> None:
    manager = SimpleNamespace(
        data={
            "energy_sources": [
                {
                    "type": "grid",
                    "stat_energy_from": "sensor.smart_meter_electricity_consumption",
                    "stat_cost": "hildebrand_glow:site_electricity_energy_cost",
                    "entity_energy_price": None,
                    "number_energy_price": None,
                    "cost_adjustment_day": 0.0,
                },
                {
                    "type": "gas",
                    "stat_energy_from": "sensor.unrelated_gas",
                    "stat_cost": None,
                    "entity_energy_price": None,
                    "number_energy_price": None,
                },
            ]
        },
        async_update=AsyncMock(),
    )

    with patch(
        "custom_components.hildebrand_glow.energy_migration.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        changed = await async_migrate_energy_consumption_statistics(
            object(),
            {
                "sensor.smart_meter_electricity_consumption": (
                    "hildebrand_glow:site_electricity_energy_consumption"
                )
            },
        )

    assert changed is True
    manager.async_update.assert_awaited_once()
    updated = manager.async_update.await_args.args[0]["energy_sources"]
    assert updated[0]["stat_energy_from"] == (
        "hildebrand_glow:site_electricity_energy_consumption"
    )
    assert updated[0]["stat_cost"] == "hildebrand_glow:site_electricity_energy_cost"
    assert updated[1]["stat_energy_from"] == "sensor.unrelated_gas"


@pytest.mark.asyncio
async def test_energy_migration_is_idempotent_when_reference_is_already_external() -> None:
    manager = SimpleNamespace(
        data={
            "energy_sources": [
                {
                    "type": "grid",
                    "stat_energy_from": "hildebrand_glow:site_electricity_energy_consumption",
                }
            ]
        },
        async_update=AsyncMock(),
    )

    with patch(
        "custom_components.hildebrand_glow.energy_migration.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        changed = await async_migrate_energy_consumption_statistics(
            object(),
            {
                "sensor.smart_meter_electricity_consumption": (
                    "hildebrand_glow:site_electricity_energy_consumption"
                )
            },
        )

    assert changed is False
    manager.async_update.assert_not_awaited()
