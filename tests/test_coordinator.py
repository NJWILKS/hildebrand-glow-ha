from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from custom_components.hildebrand_glow.api import GlowmarktApiError
from custom_components.hildebrand_glow.const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_ELECTRICITY_COST,
    CLASSIFIER_GAS_CONSUMPTION,
    CLASSIFIER_GAS_COST,
)
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
        return deepcopy(self.state)

    async def async_save(self, data: dict) -> None:
        self.state.clear()
        self.state.update(deepcopy(data))


class FakeApi:
    def __init__(self) -> None:
        self.discover_resources = AsyncMock(
            return_value={
                CLASSIFIER_ELECTRICITY_CONSUMPTION: {
                    "resource_id": "electricity-resource"
                },
                CLASSIFIER_GAS_CONSUMPTION: {"resource_id": "gas-resource"},
                CLASSIFIER_ELECTRICITY_COST: {
                    "resource_id": "electricity-cost-resource"
                },
                CLASSIFIER_GAS_COST: {"resource_id": "gas-cost-resource"},
            }
        )

        async def get_readings(classifiers: set[str] | None = None):
            all_readings = {
                CLASSIFIER_ELECTRICITY_CONSUMPTION: FakeReading(
                    "2026-09-05", 10.0, []
                ),
                CLASSIFIER_GAS_CONSUMPTION: FakeReading(
                    "2026-09-05", 20.0, []
                ),
                CLASSIFIER_ELECTRICITY_COST: FakeReading(
                    "2026-09-05", 250.0, []
                ),
                CLASSIFIER_GAS_COST: FakeReading(
                    "2026-09-05", 175.0, []
                ),
            }
            if classifiers is None:
                return all_readings
            return {
                classifier: reading
                for classifier, reading in all_readings.items()
                if classifier in classifiers
            }

        self.get_readings = AsyncMock(side_effect=get_readings)
        self.get_available_readings = AsyncMock(return_value={})


def _make_coordinator(
    hass,
    api: FakeApi,
    virtual_entity_id: str | None = None,
    entry_id: str = "default",
    consumption_interval_minutes: int = 15,
    cost_interval_minutes: int = 60,
):
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
        entry_id=entry_id,
        consumption_interval_minutes=consumption_interval_minutes,
        cost_interval_minutes=cost_interval_minutes,
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
    assert data["readings"][CLASSIFIER_ELECTRICITY_COST] == 250.0
    assert data["readings"][CLASSIFIER_GAS_COST] == 175.0


@pytest.mark.asyncio
async def test_coordinator_keeps_last_good_value_when_api_returns_none(hass) -> None:
    api = FakeApi()
    coordinator = _make_coordinator(hass, api)

    first = await coordinator._async_update_data()

    async def empty_readings(classifiers: set[str] | None = None):
        return {classifier: None for classifier in classifiers or set()}

    api.get_readings.side_effect = empty_readings
    second = await coordinator._async_update_data()

    assert second["readings"] == first["readings"]
    assert second["cumulative_readings"] == first["cumulative_readings"]


@pytest.mark.asyncio
async def test_coordinator_discovers_only_configured_virtual_entity(hass) -> None:
    api = FakeApi()
    coordinator = _make_coordinator(hass, api, virtual_entity_id="site-2")

    await coordinator._async_update_data()

    api.discover_resources.assert_awaited_once_with("site-2")


def test_coordinator_identity_survives_config_entry_recreation(hass) -> None:
    api = FakeApi()
    first = _make_coordinator(
        hass,
        api,
        virtual_entity_id="site-2",
        entry_id="entry-a",
    )
    second = _make_coordinator(
        hass,
        api,
        virtual_entity_id="site-2",
        entry_id="entry-b",
    )

    assert first._site_id == "site-2"
    assert second._site_id == "site-2"


@pytest.mark.asyncio
async def test_accumulation_counts_each_calendar_day_once_across_restart(hass) -> None:
    shared_state: dict = {}
    first_api = FakeApi()
    first = _make_coordinator(hass, first_api)
    first._store = FakeStore(shared_state)

    day_one = FakeReading("2026-09-04", 10.0, [])
    assert await first._accumulate(CLASSIFIER_ELECTRICITY_CONSUMPTION, day_one) == 10.0
    assert await first._accumulate(CLASSIFIER_ELECTRICITY_CONSUMPTION, day_one) == 10.0

    second_api = FakeApi()
    restarted = _make_coordinator(hass, second_api)
    restarted._store = FakeStore(shared_state)
    day_two = FakeReading("2026-09-05", 5.0, [])

    assert (
        await restarted._accumulate(
            CLASSIFIER_ELECTRICITY_CONSUMPTION,
            day_one,
        )
        == 10.0
    )
    assert (
        await restarted._accumulate(
            CLASSIFIER_ELECTRICITY_CONSUMPTION,
            day_two,
        )
        == 15.0
    )
    assert shared_state[CLASSIFIER_ELECTRICITY_CONSUMPTION] == {
        "day": "2026-09-05",
        "cumulative": 15.0,
    }


@pytest.mark.asyncio
async def test_api_cost_resources_refresh_less_often_than_consumption(hass) -> None:
    api = FakeApi()
    coordinator = _make_coordinator(
        hass,
        api,
        consumption_interval_minutes=15,
        cost_interval_minutes=60,
    )

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    requested = [call.args[0] for call in api.get_readings.await_args_list]
    consumption_set = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION,
        CLASSIFIER_GAS_CONSUMPTION,
    }
    cost_set = {
        CLASSIFIER_ELECTRICITY_COST,
        CLASSIFIER_GAS_COST,
    }

    assert requested.count(consumption_set) == 2
    assert requested.count(cost_set) == 1


@pytest.mark.asyncio
async def test_cost_api_failure_does_not_take_consumption_offline(hass) -> None:
    api = FakeApi()
    coordinator = _make_coordinator(hass, api)

    async def selective_failure(classifiers: set[str] | None = None):
        if classifiers == {
            CLASSIFIER_ELECTRICITY_COST,
            CLASSIFIER_GAS_COST,
        }:
            raise GlowmarktApiError("rate limited")
        return {
            CLASSIFIER_ELECTRICITY_CONSUMPTION: FakeReading(
                "2026-09-05", 10.0, []
            ),
            CLASSIFIER_GAS_CONSUMPTION: FakeReading(
                "2026-09-05", 20.0, []
            ),
        }

    api.get_readings.side_effect = selective_failure

    data = await coordinator._async_update_data()

    assert data["cumulative_readings"][CLASSIFIER_ELECTRICITY_CONSUMPTION] == 10.0
    assert data["cumulative_readings"][CLASSIFIER_GAS_CONSUMPTION] == 20.0


def test_update_settings_enforces_minimum_poll_floor(hass) -> None:
    api = FakeApi()
    coordinator = _make_coordinator(hass, api)

    coordinator.update_settings(
        coordinator.tariff_config,
        consumption_interval_minutes=1,
        cost_interval_minutes=2,
    )

    assert coordinator.consumption_interval_minutes == 5
    assert coordinator.cost_interval_minutes == 5
    assert coordinator.update_interval.total_seconds() == 300
