from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.hildebrand_glow.coordinator import GlowmarktDataUpdateCoordinator


class FakeApi:
    def __init__(self) -> None:
        self.discover_resources = AsyncMock(return_value={"electricity.consumption": {"resource_id": "electricity-resource"}, "gas.consumption": {"resource_id": "gas-resource"}})
        self.get_all_readings = AsyncMock(return_value={"electricity.consumption": 10.0, "gas.consumption": 20.0})


@pytest.mark.asyncio
async def test_coordinator_calculates_configured_daily_costs(hass) -> None:
    api = FakeApi()
    coordinator = GlowmarktDataUpdateCoordinator(hass, api, {"electricity_rate": 0.25, "electricity_standing_charge": 0.50, "gas_rate": 0.07, "gas_standing_charge": 0.30})  # type: ignore[arg-type]
    data = await coordinator._async_update_data()
    assert data["costs"]["electricity"] == 3.00
    assert data["costs"]["gas"] == 1.70
    assert data["costs"]["total"] == 4.70
    assert data["costs"]["standing_charges_total"] == 0.80


@pytest.mark.asyncio
async def test_coordinator_keeps_last_good_value_when_api_returns_none(hass) -> None:
    api = FakeApi()
    coordinator = GlowmarktDataUpdateCoordinator(hass, api, {"electricity_rate": 0.25, "electricity_standing_charge": 0.50, "gas_rate": 0.07, "gas_standing_charge": 0.30})  # type: ignore[arg-type]
    first = await coordinator._async_update_data()
    api.get_all_readings.return_value = {"electricity.consumption": None, "gas.consumption": None}
    second = await coordinator._async_update_data()
    assert second["readings"] == first["readings"]


@pytest.mark.asyncio
async def test_coordinator_discovers_only_configured_virtual_entity(hass) -> None:
    api = FakeApi()
    coordinator = GlowmarktDataUpdateCoordinator(hass, api, {"electricity_rate": 0.25, "electricity_standing_charge": 0.50, "gas_rate": 0.07, "gas_standing_charge": 0.30}, virtual_entity_id="site-2")  # type: ignore[arg-type]
    await coordinator._async_update_data()
    api.discover_resources.assert_awaited_once_with("site-2")
