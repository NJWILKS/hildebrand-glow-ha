from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from custom_components.hildebrand_glow.coordinator import GlowmarktDataUpdateCoordinator


@dataclass
class FakeReading:
    day: str
    value: float
    intervals: list[tuple[datetime, float]]


class FakeStore:
    def __init__(self, state: dict | None = None) -> None:
        self.state = state if state is not None else {}

    async def async_load(self) -> dict:
        return self.state

    async def async_save(self, data: dict) -> None:
        self.state.clear()
        self.state.update(data)


class FakeApi:
    def __init__(self) -> None:
        self.discover_resources = AsyncMock(
            return_value={
                "electricity.consumption": {"resource_id": "electricity-resource"},
                "gas.consumption": {"resource_id": "gas-resource"},
            }
        )
        self.get_all_readings = AsyncMock(
            return_value={
                "electricity.consumption": FakeReading("2026-09-05", 10.0, []),
                "gas.consumption": FakeReading("2026-09-05", 20.0, []),
            }
        )
        self.get_available_readings = AsyncMock(return_value={})


def _make_coordinator(hass, api: FakeApi, virtual_entity_id: str | None = None):
    coordinator = GlowmarktDataUpdateCoordinator(
        hass,
        api,  # type: ignore[arg-type]
        {
            "electricity_rate": 0.25,
            "electricity_standing_charge": 0.50,
            "gas_rate": 0.07,
            "gas_standing_charge": 0.30,
        },
        virtual_entity_id=virtual_entity_id,
    )
    coordinator._store = FakeStore()
    coordinator._backfill_started = True
    return coordinator


@pytest.mark.asyncio
async def test_coordinator_calculates_configured_daily_costs(hass) -> None:
    api = FakeApi()
    coordinator = _make_coordinator(hass, api)

    data = await coordinator._async_update_data()

    assert data["costs"]["electricity"] == 3.00
    assert data["costs"]["gas"] == 1.70
    assert data["costs"]["total"] == 4.70
    assert data["costs"]["standing_charges_total"] == 0.80


@pytest.mark.asyncio
async def test_coordinator_keeps_last_good_value_when_api_returns_none(hass) -> None:
    api = FakeApi()
    coordinator = _make_coordinator(hass, api)

    first = await coordinator._async_update_data()
    api.get_all_readings.return_value = {
        "electricity.consumption": None,
        "gas.consumption": None,
    }
    second = await coordinator._async_update_data()

    assert second["readings"] == first["readings"]
    assert second["cumulative_readings"] == first["cumulative_readings"]


@pytest.mark.asyncio
async def test_coordinator_discovers_only_configured_virtual_entity(hass) -> None:
    api = FakeApi()
    coordinator = _make_coordinator(hass, api, virtual_entity_id="site-2")

    await coordinator._async_update_data()

    api.discover_resources.assert_awaited_once_with("site-2")


@pytest.mark.asyncio
async def test_accumulation_counts_each_calendar_day_once_across_restart(hass) -> None:
    shared_state: dict = {}
    first_api = FakeApi()
    first = _make_coordinator(hass, first_api)
    first._store = FakeStore(shared_state)

    day_one = FakeReading("2026-09-04", 10.0, [])
    assert await first._accumulate("electricity.consumption", day_one) == 10.0
    assert await first._accumulate("electricity.consumption", day_one) == 10.0

    second_api = FakeApi()
    restarted = _make_coordinator(hass, second_api)
    restarted._store = FakeStore(shared_state)
    day_two = FakeReading("2026-09-05", 5.0, [])

    assert await restarted._accumulate("electricity.consumption", day_one) == 10.0
    assert await restarted._accumulate("electricity.consumption", day_two) == 15.0
    assert shared_state["electricity.consumption"] == {
        "day": "2026-09-05",
        "cumulative": 15.0,
    }
