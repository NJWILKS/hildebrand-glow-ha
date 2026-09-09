from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import custom_components.hildebrand_glow.cost_ingestion as ingestion
from custom_components.hildebrand_glow.api import UK_TZ, DailyReading, GlowmarktApiError
from custom_components.hildebrand_glow.const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_ELECTRICITY_COST,
)
from custom_components.hildebrand_glow.costing import CostBreakdown


class FakeStore:
    def __init__(self, state: dict | None = None) -> None:
        self.state = dict(state or {})
        self.saved: list[dict] = []

    async def async_load(self) -> dict:
        return dict(self.state)

    async def async_save(self, data: dict) -> None:
        self.state = dict(data)
        self.saved.append(dict(data))


def _breakdown(day: str, *, complete: bool = True) -> CostBreakdown:
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return CostBreakdown(
        day=day,
        total_pence=90.0,
        usage_pence=40.0,
        standing_charge_pence=50.0,
        standing_charge_status="applied" if complete else "not_applied",
        complete_day=complete,
        usage_intervals=((start, 20.0), (start.replace(minute=30), 20.0)),
    )


def _reading(day: str) -> DailyReading:
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return DailyReading(
        day=day,
        value=2.0,
        intervals=[(start, 1.0), (start.replace(minute=30), 1.0)],
    )


def _tariff_row(day: str = "2026-09-01 00:00:00") -> dict:
    return {
        "effectiveDate": day,
        "plan": [{"planDetail": [{"rate": 25.0}, {"standing": 50.0}]}],
    }


@pytest.mark.asyncio
async def test_tariff_ledger_refresh_sorts_filters_and_preserves_analysis(hass) -> None:
    store = FakeStore({"analysis": {"keep": True}})
    api = SimpleNamespace(
        _get_json=AsyncMock(
            return_value={
                "status": "OK",
                "data": [
                    _tariff_row("2026-09-02 00:00:00"),
                    "ignore-me",
                    _tariff_row("2026-09-01 00:00:00"),
                ],
            }
        )
    )
    coordinator = SimpleNamespace(
        resources={CLASSIFIER_ELECTRICITY_COST: {"resource_id": "cost-resource"}},
        api_client=api,
    )

    with patch.object(ingestion, "Store", return_value=store):
        result = await ingestion._refresh_tariff_ledger(hass, coordinator, "site-123")

    rows = result["commodities"]["electricity"]
    assert [row["effectiveDate"] for row in rows] == [
        "2026-09-01 00:00:00",
        "2026-09-02 00:00:00",
    ]
    assert result["analysis"] == {"keep": True}
    assert store.saved[-1] == result


@pytest.mark.asyncio
async def test_tariff_ledger_refresh_rejects_api_error(hass) -> None:
    api = SimpleNamespace(_get_json=AsyncMock(return_value={"status": "ERROR"}))
    coordinator = SimpleNamespace(
        resources={CLASSIFIER_ELECTRICITY_COST: {"resource_id": "cost-resource"}},
        api_client=api,
    )

    with pytest.raises(GlowmarktApiError):
        await ingestion._refresh_tariff_ledger(hass, coordinator, "site-123")


@pytest.mark.asyncio
async def test_full_reconcile_rebuilds_all_owned_cost_statistics(hass) -> None:
    store = FakeStore()
    raw = _breakdown("2026-09-07")
    writes: list[tuple[dict, list[dict]]] = []
    clear = AsyncMock()
    coordinator = SimpleNamespace(
        resources={
            CLASSIFIER_ELECTRICITY_COST: {"resource_id": "cost-resource"},
            CLASSIFIER_ELECTRICITY_CONSUMPTION: {
                "resource_id": "consumption-resource"
            },
        },
        api_client=object(),
        tariff_config={
            "electricity_rate": 0.25,
            "electricity_standing_charge": 0.50,
        },
    )

    def capture(_hass, metadata, stats) -> None:
        writes.append((metadata, list(stats)))

    with (
        patch.object(
            ingestion,
            "_load_store",
            new=AsyncMock(return_value=(store, store.state)),
        ),
        patch.object(
            ingestion,
            "get_cost_history",
            new=AsyncMock(return_value=[raw]),
        ) as history,
        patch.object(
            ingestion,
            "_consumption_history",
            new=AsyncMock(return_value=[_reading("2026-09-07")]),
        ),
        patch.object(
            ingestion,
            "_entity_id",
            new=AsyncMock(side_effect=["sensor.usage", "sensor.standing"]),
        ),
        patch.object(ingestion, "_clear_statistics", new=clear),
        patch.object(ingestion, "async_add_external_statistics", side_effect=capture),
        patch.object(
            ingestion,
            "_save_tariff_analysis",
            new=AsyncMock(),
        ) as save_analysis,
    ):
        result = await ingestion._reconcile_locked(
            hass,
            coordinator,
            "site-123",
            {"commodities": {"electricity": [_tariff_row()]}},
        )

    assert result is True
    history.assert_awaited_once_with(
        coordinator.api_client,
        "cost-resource",
        start_uk=None,
    )
    assert clear.await_count == 1
    cleared_ids = clear.await_args.args[1]
    assert ingestion.energy_cost_statistic_id("site-123", "electricity") in cleared_ids
    assert (
        ingestion.cost_component_statistic_id("site-123", "electricity", "usage_cost")
        in cleared_ids
    )
    assert (
        ingestion.cost_component_statistic_id(
            "site-123",
            "electricity",
            "standing_charge",
        )
        in cleared_ids
    )
    assert "sensor.usage" in cleared_ids
    assert "sensor.standing" in cleared_ids

    assert len(writes) == 3
    ids = {metadata["statistic_id"] for metadata, _stats in writes}
    assert ids == {
        ingestion.energy_cost_statistic_id("site-123", "electricity"),
        ingestion.cost_component_statistic_id(
            "site-123",
            "electricity",
            "usage_cost",
        ),
        ingestion.cost_component_statistic_id(
            "site-123",
            "electricity",
            "standing_charge",
        ),
    }
    assert store.state["_ingestion_version"] == ingestion.COST_INGESTION_SCHEMA_VERSION
    assert store.state["commodities"]["electricity"] == {
        "last_completed_day": "2026-09-07",
        "completed_total_sum_gbp": 1.0,
        "usage_sum_gbp": 0.5,
        "standing_sum_gbp": 0.5,
    }
    save_analysis.assert_awaited_once()


@pytest.mark.asyncio
async def test_incremental_reconcile_uses_previous_day_and_does_not_clear(hass) -> None:
    state = {
        "_ingestion_version": ingestion.COST_INGESTION_SCHEMA_VERSION,
        "commodities": {
            "electricity": {
                "last_completed_day": "2026-09-06",
                "completed_total_sum_gbp": 10.0,
                "usage_sum_gbp": 6.0,
                "standing_sum_gbp": 4.0,
            }
        },
    }
    store = FakeStore(state)
    clear = AsyncMock()
    coordinator = SimpleNamespace(
        resources={CLASSIFIER_ELECTRICITY_COST: {"resource_id": "cost-resource"}},
        api_client=object(),
        tariff_config={
            "electricity_rate": 0.25,
            "electricity_standing_charge": 0.50,
        },
    )

    with (
        patch.object(
            ingestion,
            "_load_store",
            new=AsyncMock(return_value=(store, state)),
        ),
        patch.object(
            ingestion,
            "get_cost_history",
            new=AsyncMock(return_value=[_breakdown("2026-09-07")]),
        ) as history,
        patch.object(ingestion, "_clear_statistics", new=clear),
        patch.object(ingestion, "async_add_external_statistics"),
        patch.object(ingestion, "_save_tariff_analysis", new=AsyncMock()),
    ):
        await ingestion._reconcile_locked(
            hass,
            coordinator,
            "site-123",
            {"commodities": {"electricity": [_tariff_row()]}},
        )

    assert history.await_args.kwargs["start_uk"].date().isoformat() == "2026-09-07"
    clear.assert_not_awaited()
    assert store.state["commodities"]["electricity"]["completed_total_sum_gbp"] > 10.0


@pytest.mark.asyncio
async def test_open_day_publishes_total_usage_and_standing_from_closed_baseline(hass) -> None:
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UK_TZ)
    state = {
        "_ingestion_version": ingestion.COST_INGESTION_SCHEMA_VERSION,
        "commodities": {
            "electricity": {
                "last_completed_day": "2026-09-07",
                "completed_total_sum_gbp": 10.0,
                "usage_sum_gbp": 6.0,
                "standing_sum_gbp": 4.0,
            }
        },
    }
    open_breakdown = _breakdown("2026-09-08", complete=False)
    writes: list[tuple[dict, list[dict]]] = []
    coordinator = SimpleNamespace(
        resources={
            CLASSIFIER_ELECTRICITY_COST: {"resource_id": "cost-resource"},
            CLASSIFIER_ELECTRICITY_CONSUMPTION: {
                "resource_id": "consumption-resource"
            },
        },
        cost_breakdowns={"electricity": open_breakdown},
        tariff_config={
            "electricity_rate": 0.25,
            "electricity_standing_charge": 0.50,
        },
    )

    def capture(_hass, metadata, stats) -> None:
        writes.append((metadata, list(stats)))

    with (
        patch.object(
            ingestion,
            "_load_store",
            new=AsyncMock(return_value=(FakeStore(state), state)),
        ),
        patch.object(
            ingestion,
            "_open_day_consumption",
            new=AsyncMock(return_value=_reading("2026-09-08")),
        ),
        patch.object(
            ingestion,
            "async_add_external_statistics",
            side_effect=capture,
        ),
    ):
        await ingestion._import_open_day_locked(
            hass,
            coordinator,
            "site-123",
            now,
            {"commodities": {"electricity": [_tariff_row()]}},
        )

    assert len(writes) == 3
    by_id = {metadata["statistic_id"]: stats for metadata, stats in writes}
    total = by_id[ingestion.energy_cost_statistic_id("site-123", "electricity")]
    usage = by_id[
        ingestion.cost_component_statistic_id(
            "site-123",
            "electricity",
            "usage_cost",
        )
    ]
    standing = by_id[
        ingestion.cost_component_statistic_id(
            "site-123",
            "electricity",
            "standing_charge",
        )
    ]
    assert total[-1]["sum"] == 11.0
    assert usage[-1]["sum"] == 6.5
    assert standing[-1]["sum"] == 4.5


@pytest.mark.asyncio
async def test_sync_runs_closed_reconcile_then_open_day_under_one_lock(hass) -> None:
    events: list[str] = []
    coordinator = SimpleNamespace(cost_history_lock=asyncio.Lock())
    ledger = {"commodities": {}}

    async def reconcile(*_args) -> bool:
        assert coordinator.cost_history_lock.locked()
        events.append("closed")
        return True

    async def open_day(*_args) -> None:
        assert coordinator.cost_history_lock.locked()
        events.append("open")

    with (
        patch.object(ingestion, "_reconcile_locked", side_effect=reconcile),
        patch.object(ingestion, "_import_open_day_locked", side_effect=open_day),
    ):
        await ingestion.async_sync_cost_ingestion(
            hass,
            coordinator,
            "site-123",
            tariff_ledger=ledger,
        )

    assert events == ["closed", "open"]
