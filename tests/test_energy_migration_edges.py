from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.hildebrand_glow.energy_migration import (
    _bind_owned_cost,
    _matching_cost_statistic,
    async_migrate_energy_consumption_statistics,
)


def test_matching_cost_statistic_rejects_non_owned_and_non_string_sources() -> None:
    assert _matching_cost_statistic(None) is None
    assert _matching_cost_statistic(123) is None
    assert _matching_cost_statistic("sensor.energy_consumption") is None
    assert _matching_cost_statistic("hildebrand_glow:site_energy") is None
    assert (
        _matching_cost_statistic("hildebrand_glow:site_gas_energy_consumption")
        == "hildebrand_glow:site_gas_energy_cost"
    )


def test_bind_owned_cost_leaves_unrelated_or_explicit_cost_sources_unchanged() -> None:
    unrelated = {
        "stat_energy_from": "sensor.other",
        "stat_cost": None,
        "number_energy_price": 0.1,
    }
    assert _bind_owned_cost(unrelated) is False
    assert unrelated["number_energy_price"] == 0.1

    explicit = {
        "stat_energy_from": "hildebrand_glow:site_electricity_energy_consumption",
        "stat_cost": "sensor.custom_cost",
        "entity_energy_price": "sensor.price",
    }
    assert _bind_owned_cost(explicit) is False
    assert explicit["entity_energy_price"] == "sensor.price"


@pytest.mark.asyncio
async def test_energy_migration_returns_false_when_energy_preferences_not_loaded() -> None:
    manager = SimpleNamespace(data=None, async_update=AsyncMock())

    with patch(
        "custom_components.hildebrand_glow.energy_migration.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        changed = await async_migrate_energy_consumption_statistics(object(), {})

    assert changed is False
    manager.async_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_energy_migration_handles_empty_and_non_list_legacy_flow_shapes() -> None:
    manager = SimpleNamespace(
        data={
            "energy_sources": [
                {"type": "solar"},
                {"type": "grid", "flow_from": None},
                {"type": "grid", "flow_from": "not-a-list"},
            ]
        },
        async_update=AsyncMock(),
    )

    with patch(
        "custom_components.hildebrand_glow.energy_migration.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        changed = await async_migrate_energy_consumption_statistics(object(), {})

    assert changed is False
    manager.async_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_energy_migration_skips_non_dict_flow_and_migrates_valid_legacy_flow() -> None:
    manager = SimpleNamespace(
        data={
            "energy_sources": [
                {
                    "type": "grid",
                    "flow_from": [
                        "not-a-dict",
                        {
                            "stat_energy_from": "sensor.old_gas",
                            "stat_cost": None,
                            "entity_energy_price": "sensor.old_price",
                            "number_energy_price": 0.06,
                        },
                    ],
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
            {"sensor.old_gas": "hildebrand_glow:site_gas_energy_consumption"},
        )

    assert changed is True
    updated = manager.async_update.await_args.args[0]["energy_sources"][0]["flow_from"]
    assert updated[0] == "not-a-dict"
    assert updated[1]["stat_energy_from"] == "hildebrand_glow:site_gas_energy_consumption"
    assert updated[1]["stat_cost"] == "hildebrand_glow:site_gas_energy_cost"
    assert updated[1]["entity_energy_price"] is None
    assert updated[1]["number_energy_price"] is None
