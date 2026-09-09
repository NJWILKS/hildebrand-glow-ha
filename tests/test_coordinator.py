from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from custom_components.hildebrand_glow.api import GlowmarktApiError
from custom_components.hildebrand_glow.const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_ELECTRICITY_COST,
    CLASSIFIER_GAS_CONSUMPTION,
    CLASSIFIER_GAS_COST,
)
from custom_components.hildebrand_glow.coordinator import (
    CUMULATIVE_BACKFILL_SCHEMA_VERSION,
    GlowmarktDataUpdateCoordinator,
)


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

        async def fetch_day_reading(
            resource_id: str,
            start: datetime,
            _end: datetime,
            _days_back: int | None = None,
        ) -> FakeReading | None:
            values = {
                "electricity-resource": 10.0,
                "gas-resource": 20.0,
            }
            value = values.get(resource_id)
            if value is None:
                return None
            return FakeReading(
                day=start.date().isoformat(),
                value=value,
                intervals=[(start.astimezone(timezone.utc), value)],
            )

        async def request_readings(
            resource_id: str,
            start: datetime,
            end: datetime,
            period: str,
        ):
            timestamp = int(start.astimezone(timezone.utc).timestamp())
            if resource_id == "electricity-cost-resource":
                value = 250.0 if period == "PT30M" else 300.0
            elif resource_id == "gas-cost-resource":
                value = 140.0 if period == "PT30M" else 170.0
            else:
                return []
            return [[timestamp, value]]

        self._fetch_day_reading = AsyncMock(side_effect=fetch_day_reading)
        self._request_readings = AsyncMock(side_effect=request_readings)


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


def _seed_ready_consumption(
    coordinator: GlowmarktDataUpdateCoordinator,
    completed_day: str = "2026-09-06",
) -> None:
    coordinator._store = FakeStore(
        {
            "_backfilled": True,
            "_backfilled_version": CUMULATIVE_BACKFILL_SCHEMA_VERSION,
            CLASSIFIER_ELECTRICITY_CONSUMPTION: {
                "day": completed_day,
                "completed_day": completed_day,
                "completed_cumulative": 0.0,
                "cumulative": 0.0,
                "live_day": None,
                "live_intervals": 0,
            },
            CLASSIFIER_GAS_CONSUMPTION: {
                "day": completed_day,
                "completed_day": completed_day,
                "completed_cumulative": 0.0,
                "cumulative": 0.0,
                "live_day": None,
                "live_intervals": 0,
            },
        }
    )
    coordinator._entity_id_for = lambda classifier: f"sensor.{classifier.replace('.', '_')}"
    coordinator._add_consumption_statistics = (
        lambda _classifier, reading, baseline: round(baseline + reading.value, 3)
    )


@pytest.mark.asyncio
async def test_partial_day_uses_api_cost_without_standing_charge(hass, freezer) -> None:
    freezer.move_to("2026-09-07 12:00:00+01:00")
    api = FakeApi()
    coordinator = _make_coordinator(hass, api)

    data = await coordinator._async_update_data()

    assert data["costs"]["electricity"] == 2.50
    assert data["costs"]["gas"] == 1.40
    assert data["costs"]["total"] == 3.90
    assert data["costs"]["electricity_standing_charge"] == 0.0
    assert data["costs"]["gas_standing_charge"] == 0.0
    assert data["costs"]["standing_charges_total"] == 0.0
    assert data["readings"][CLASSIFIER_ELECTRICITY_COST] == 250.0
    assert data["readings"][CLASSIFIER_GAS_COST] == 140.0
    assert data["cost_diagnostics"]["electricity"] == {
        "source": "glow_api",
        "api_day": "2026-09-07",
        "complete_day": False,
        "usage_cost_gbp": 2.5,
        "standing_charge_status": "not_applied",
        "pt30m_intervals": 1,
    }


@pytest.mark.asyncio
async def test_completed_day_derives_standing_from_p1d_minus_pt30m(hass, freezer) -> None:
    freezer.move_to("2026-09-07 00:00:00+01:00")
    api = FakeApi()
    coordinator = _make_coordinator(hass, api)

    data = await coordinator._async_update_data()

    assert data["costs"]["electricity"] == 3.00
    assert data["costs"]["gas"] == 1.70
    assert data["costs"]["total"] == 4.70
    assert data["costs"]["electricity_standing_charge"] == 0.50
    assert data["costs"]["gas_standing_charge"] == 0.30
    assert data["costs"]["standing_charges_total"] == 0.80
    assert data["cost_diagnostics"]["electricity"]["standing_charge_status"] == "applied"
    periods = [call.args[3] for call in api._request_readings.await_args_list]
    assert periods == ["PT30M", "P1D", "PT30M", "P1D"]


@pytest.mark.asyncio
async def test_api_cost_failure_falls_back_without_taking_consumption_offline(
    hass,
    freezer,
) -> None:
    freezer.move_to("2026-09-07 12:00:00+01:00")
    api = FakeApi()
    coordinator = _make_coordinator(hass, api)
    _seed_ready_consumption(coordinator)
    api._request_readings.side_effect = GlowmarktApiError("rate limited")

    data = await coordinator._async_update_data()

    assert data["cumulative_readings"][CLASSIFIER_ELECTRICITY_CONSUMPTION] == 10.0
    assert data["cumulative_readings"][CLASSIFIER_GAS_CONSUMPTION] == 20.0
    assert data["costs"]["electricity"] == 3.00
    assert data["costs"]["gas"] == 1.70
    assert data["costs"]["standing_charges_total"] == 0.80
    assert data["cost_diagnostics"]["electricity"]["source"] == "configured_fallback"


@pytest.mark.asyncio
async def test_electricity_only_site_does_not_add_gas_standing_charge(hass, freezer) -> None:
    freezer.move_to("2026-09-07 00:00:00+01:00")
    api = FakeApi()
    api.discover_resources.return_value = {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {"resource_id": "electricity-resource"},
        CLASSIFIER_ELECTRICITY_COST: {"resource_id": "electricity-cost-resource"},
    }
    coordinator = _make_coordinator(hass, api)

    data = await coordinator._async_update_data()

    assert data["costs"]["electricity_standing_charge"] == 0.50
    assert "gas_standing_charge" not in data["costs"]
    assert data["costs"]["standing_charges_total"] == 0.50


@pytest.mark.asyncio
async def test_coordinator_keeps_last_good_value_when_consumption_returns_none(
    hass,
    freezer,
) -> None:
    freezer.move_to("2026-09-07 12:00:00+01:00")
    api = FakeApi()
    coordinator = _make_coordinator(hass, api)
    _seed_ready_consumption(coordinator)

    first = await coordinator._async_update_data()

    api._fetch_day_reading.side_effect = lambda *_args, **_kwargs: None
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
async def test_api_cost_resources_refresh_less_often_than_consumption(
    hass,
    freezer,
) -> None:
    freezer.move_to("2026-09-07 12:00:00+01:00")
    api = FakeApi()
    coordinator = _make_coordinator(
        hass,
        api,
        consumption_interval_minutes=15,
        cost_interval_minutes=60,
    )
    _seed_ready_consumption(coordinator)

    await coordinator._async_update_data()
    first_cost_calls = api._request_readings.await_count
    await coordinator._async_update_data()

    requested_resources = [
        call.args[0] for call in api._fetch_day_reading.await_args_list
    ]
    assert requested_resources.count("electricity-resource") == 2
    assert requested_resources.count("gas-resource") == 2
    assert api._request_readings.await_count == first_cost_calls


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
