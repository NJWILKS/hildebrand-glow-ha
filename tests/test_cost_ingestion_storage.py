from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_recorder_block_till_done,
)

import custom_components.hildebrand_glow.cost_ingestion as ingestion


class FakeStore:
    def __init__(self, loaded=None) -> None:
        self.loaded = loaded
        self.saved: list[dict] = []

    async def async_load(self):
        return self.loaded

    async def async_save(self, data: dict) -> None:
        self.saved.append(data)
        self.loaded = data


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url: str) -> None:
    """Prepare the real Recorder before Home Assistant starts."""
    assert recorder_db_url


@pytest.mark.asyncio
async def test_cost_clear_statistics_empty_and_recorder_callback_paths(recorder_mock, hass) -> None:
    await ingestion._clear_statistics(hass, [])
    await ingestion._clear_statistics(hass, ["hildebrand_glow:nonexistent_cost_stat"])
    await async_recorder_block_till_done(hass)


@pytest.mark.asyncio
async def test_entity_id_resolves_legacy_sensor_unique_id(hass) -> None:
    registry = MagicMock()
    registry.async_get_entity_id.return_value = "sensor.legacy_cost"

    with patch.object(ingestion.er, "async_get", return_value=registry):
        result = await ingestion._entity_id(hass, "site-123", "electricity_usage_cost")

    assert result == "sensor.legacy_cost"
    args = registry.async_get_entity_id.call_args.args
    assert args[0] == "sensor"
    assert args[1] == ingestion.DOMAIN
    assert "site-123" in args[2]


@pytest.mark.asyncio
async def test_store_helpers_load_empty_and_existing_state(hass) -> None:
    cost_store = FakeStore(None)
    tariff_store = FakeStore({"commodities": {"electricity": []}})

    with patch.object(ingestion, "Store", side_effect=[cost_store, tariff_store]) as store_type:
        store, state = await ingestion._load_store(hass, "site-123")
        ledger = await ingestion._load_tariff_ledger(hass, "site-123")

    assert store is cost_store
    assert state == {}
    assert ledger == {"commodities": {"electricity": []}}
    assert store_type.call_count == 2


@pytest.mark.asyncio
async def test_save_tariff_analysis_preserves_existing_ledger(hass) -> None:
    store = FakeStore({"commodities": {"electricity": [{"effectiveDate": "2026-07-01"}]}})
    analysis = {"electricity": [{"standing_source": "tariff_list"}]}

    with patch.object(ingestion, "Store", return_value=store):
        await ingestion._save_tariff_analysis(hass, "site-123", analysis)

    assert store.saved[-1]["commodities"]["electricity"] == [
        {"effectiveDate": "2026-07-01"}
    ]
    assert store.saved[-1]["analysis"] == analysis


def test_effective_key_uses_supported_date_fields_and_empty_fallback() -> None:
    assert ingestion._effective_key({"effectiveDate": "2026-01-01"}) == "2026-01-01"
    assert ingestion._effective_key({"from": "2026-02-01"}) == "2026-02-01"
    assert ingestion._effective_key({"effective": "2026-03-01"}) == "2026-03-01"
    assert ingestion._effective_key({}) == ""


@pytest.mark.asyncio
async def test_worker_executes_recurring_tariff_refresh_and_cost_sync(hass) -> None:
    coordinator = SimpleNamespace()
    first_ledger = {"commodities": {"electricity": []}}
    second_ledger = {"commodities": {"electricity": [{"effectiveDate": "2026-07-01"}]}}
    refresh = AsyncMock(side_effect=[first_ledger, second_ledger])
    sync = AsyncMock()
    sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])

    with (
        patch.object(ingestion, "COST_HISTORY_REFRESH_SECONDS", 1),
        patch.object(ingestion, "TARIFF_REFRESH_SECONDS", 1),
        patch.object(ingestion.asyncio, "sleep", new=sleep),
        patch.object(ingestion, "_refresh_tariff_ledger", new=refresh),
        patch.object(ingestion, "async_sync_cost_ingestion", new=sync),
    ):
        with pytest.raises(asyncio.CancelledError):
            await ingestion.async_cost_ingestion_worker(hass, coordinator, "site-123")

    assert refresh.await_count == 2
    assert sync.await_count == 2
    assert sync.await_args_list[0].kwargs["tariff_ledger"] == first_ledger
    assert sync.await_args_list[1].kwargs["tariff_ledger"] == second_ledger


@pytest.mark.asyncio
async def test_worker_survives_recurring_refresh_and_sync_failures(hass) -> None:
    coordinator = SimpleNamespace()
    initial_ledger = {"commodities": {}}
    refresh = AsyncMock(side_effect=[initial_ledger, RuntimeError("refresh failed")])
    sync = AsyncMock(side_effect=[None, RuntimeError("sync failed")])
    sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])

    with (
        patch.object(ingestion, "COST_HISTORY_REFRESH_SECONDS", 1),
        patch.object(ingestion, "TARIFF_REFRESH_SECONDS", 1),
        patch.object(ingestion.asyncio, "sleep", new=sleep),
        patch.object(ingestion, "_refresh_tariff_ledger", new=refresh),
        patch.object(ingestion, "async_sync_cost_ingestion", new=sync),
    ):
        with pytest.raises(asyncio.CancelledError):
            await ingestion.async_cost_ingestion_worker(hass, coordinator, "site-123")

    assert refresh.await_count == 2
    assert sync.await_count == 2
    assert sync.await_args_list[1].kwargs["tariff_ledger"] is None
