from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import custom_components.hildebrand_glow.cost_ingestion as ingestion
from custom_components.hildebrand_glow.api import UK_TZ, DailyReading
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


def _breakdown(
    day: str = "2026-09-07",
    *,
    complete: bool = True,
    intervals: bool = True,
    usage_pence: float | None = 40.0,
    standing_pence: float | None = 50.0,
    status: str | None = None,
) -> CostBreakdown:
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    usage_intervals = (
        ((start, 20.0), (start + timedelta(minutes=30), 20.0))
        if intervals
        else ()
    )
    return CostBreakdown(
        day=day,
        total_pence=float((usage_pence or 0.0) + (standing_pence or 0.0)),
        usage_pence=usage_pence,
        standing_charge_pence=standing_pence,
        standing_charge_status=status or ("applied" if complete else "not_applied"),
        complete_day=complete,
        usage_intervals=usage_intervals,
    )


def _coordinator() -> SimpleNamespace:
    return SimpleNamespace(
        resources={
            CLASSIFIER_ELECTRICITY_COST: {"resource_id": "cost-resource"},
            CLASSIFIER_ELECTRICITY_CONSUMPTION: {
                "resource_id": "consumption-resource"
            },
        },
        api_client=SimpleNamespace(),
        tariff_config={
            "electricity_rate": 0.25,
            "electricity_standing_charge": 0.50,
        },
        cost_breakdowns={},
        cost_history_lock=asyncio.Lock(),
    )


def test_cost_component_id_rejects_unknown_component() -> None:
    with pytest.raises(ValueError, match="Unsupported cost component"):
        ingestion.cost_component_statistic_id("site", "electricity", "daily")


def test_stat_builders_cover_no_interval_and_missing_component_shapes() -> None:
    total = ingestion.build_total_cost_statistics(
        [_breakdown(intervals=False)],
        baseline=2.0,
    )
    assert len(total) == 1
    assert total[0]["state"] == 0.9
    assert total[0]["sum"] == 2.9

    usage, standing = ingestion.build_component_statistics(
        [_breakdown(intervals=False, usage_pence=None, standing_pence=50.0)],
        usage_baseline=1.0,
        standing_baseline=2.0,
    )
    assert usage == []
    assert standing[0]["state"] == 0.5
    assert standing[0]["sum"] == 2.5

    usage, standing = ingestion.build_component_statistics(
        [_breakdown(intervals=False, usage_pence=40.0, standing_pence=None)],
        usage_baseline=1.0,
        standing_baseline=2.0,
    )
    assert usage[0]["state"] == 0.4
    assert usage[0]["sum"] == 1.4
    assert standing == []


@pytest.mark.asyncio
async def test_consumption_history_uses_full_api_when_no_incremental_start() -> None:
    expected = [DailyReading(day="2026-09-01", value=1.0, intervals=[])]
    api_client = SimpleNamespace(
        get_available_daily_readings=AsyncMock(return_value=expected)
    )
    coordinator = SimpleNamespace(api_client=api_client)

    result = await ingestion._consumption_history(
        coordinator,
        "resource",
        start_uk=None,
    )

    assert result == expected
    api_client.get_available_daily_readings.assert_awaited_once_with("resource")


@pytest.mark.asyncio
async def test_consumption_history_chunks_incremental_window(freezer) -> None:
    freezer.move_to("2026-09-20 12:00:00+01:00")
    first = DailyReading(day="2026-09-01", value=1.0, intervals=[])
    second = DailyReading(day="2026-09-10", value=2.0, intervals=[])
    fetch = AsyncMock(side_effect=[[first], [second], []])
    coordinator = SimpleNamespace(api_client=SimpleNamespace(_fetch_history_chunk=fetch))

    result = await ingestion._consumption_history(
        coordinator,
        "resource",
        start_uk=datetime(2026, 9, 1, 15, 0, tzinfo=UK_TZ),
    )

    assert result == [first, second]
    assert fetch.await_count == 3
    first_call = fetch.await_args_list[0].args
    assert first_call[1].hour == 0
    assert first_call[2] - first_call[1] == timedelta(days=ingestion.HISTORY_INTERVAL_DAYS)


def test_pricing_reconciliation_logs_only_actionable_complete_deltas(caplog) -> None:
    diagnostics = [
        {
            "day": "2026-09-01",
            "complete_day": False,
            "glow_reconciliation_delta_pence": 99.0,
        },
        {
            "day": "2026-09-02",
            "complete_day": True,
            "glow_reconciliation_delta_pence": None,
        },
        {
            "day": "2026-09-03",
            "complete_day": True,
            "glow_reconciliation_delta_pence": 4.0,
        },
        {
            "day": "2026-09-04",
            "complete_day": True,
            "glow_reconciliation_delta_pence": 6.0,
            "usage_source": "tariff_flat_x_consumption",
            "standing_source": "tariff_list",
        },
    ]

    with caplog.at_level("WARNING"):
        ingestion._log_pricing_reconciliation("electricity", diagnostics)

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "6.000p" in messages[0]
    assert "2026-09-04" in messages[0]


@pytest.mark.asyncio
async def test_reconcile_loads_ledger_and_saves_schema_when_no_cost_resource(hass) -> None:
    store = FakeStore()
    coordinator = _coordinator()
    coordinator.resources = {}
    load_ledger = AsyncMock(return_value={"commodities": {}})

    with (
        patch.object(
            ingestion,
            "_load_store",
            new=AsyncMock(return_value=(store, store.state)),
        ),
        patch.object(ingestion, "_load_tariff_ledger", new=load_ledger),
        patch.object(ingestion, "get_cost_history", new=AsyncMock()) as history,
    ):
        assert await ingestion._reconcile_locked(
            hass,
            coordinator,
            "site-123",
            None,
        ) is True

    load_ledger.assert_awaited_once_with(hass, "site-123")
    history.assert_not_awaited()
    assert store.state["_ingestion_version"] == ingestion.COST_INGESTION_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_reconcile_skips_empty_history_without_writing_statistics(hass) -> None:
    store = FakeStore()
    coordinator = _coordinator()

    with (
        patch.object(
            ingestion,
            "_load_store",
            new=AsyncMock(return_value=(store, store.state)),
        ),
        patch.object(
            ingestion,
            "get_cost_history",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(ingestion, "async_add_external_statistics") as write,
        patch.object(ingestion, "_clear_statistics", new=AsyncMock()) as clear,
        patch.object(ingestion, "_save_tariff_analysis", new=AsyncMock()) as save_analysis,
    ):
        await ingestion._reconcile_locked(
            hass,
            coordinator,
            "site-123",
            {"commodities": {}},
        )

    write.assert_not_called()
    clear.assert_not_awaited()
    save_analysis.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_reconcile_works_without_consumption_or_legacy_entities(hass) -> None:
    store = FakeStore()
    coordinator = _coordinator()
    coordinator.resources = {
        CLASSIFIER_ELECTRICITY_COST: {"resource_id": "cost-resource"}
    }
    raw = _breakdown(intervals=False)
    entity = AsyncMock(return_value=None)
    clear = AsyncMock()

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
        ),
        patch.object(ingestion, "_entity_id", new=entity),
        patch.object(ingestion, "_clear_statistics", new=clear),
        patch.object(ingestion, "async_add_external_statistics"),
        patch.object(ingestion, "_save_tariff_analysis", new=AsyncMock()),
    ):
        await ingestion._reconcile_locked(
            hass,
            coordinator,
            "site-123",
            {"commodities": {"electricity": "not-a-list"}},
        )

    assert entity.await_count == 2
    cleared = clear.await_args.args[1]
    assert len(cleared) == 3
    assert all(not item.startswith("sensor.") for item in cleared)


@pytest.mark.asyncio
async def test_open_day_returns_early_until_completed_baseline_exists(hass) -> None:
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UK_TZ)
    coordinator = _coordinator()
    write = patch.object(ingestion, "async_add_external_statistics")

    states = [
        {},
        {
            "_ingestion_version": ingestion.COST_INGESTION_SCHEMA_VERSION,
            "commodities": {"electricity": {"last_completed_day": "2026-09-06"}},
        },
    ]
    for state in states:
        with (
            patch.object(
                ingestion,
                "_load_store",
                new=AsyncMock(return_value=(FakeStore(state), state)),
            ),
            write as add,
        ):
            await ingestion._import_open_day_locked(
                hass,
                coordinator,
                "site-123",
                now,
                {"commodities": {}},
            )
            add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "breakdown",
    [
        None,
        _breakdown("2026-09-08", complete=True),
        _breakdown("2026-09-07", complete=False),
        _breakdown("2026-09-08", complete=False, intervals=False),
    ],
)
async def test_open_day_rejects_non_publishable_breakdowns(hass, breakdown) -> None:
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UK_TZ)
    state = {
        "_ingestion_version": ingestion.COST_INGESTION_SCHEMA_VERSION,
        "commodities": {"electricity": {"last_completed_day": "2026-09-07"}},
    }
    coordinator = _coordinator()
    coordinator.cost_breakdowns = {"electricity": breakdown} if breakdown else {}

    with (
        patch.object(
            ingestion,
            "_load_store",
            new=AsyncMock(return_value=(FakeStore(state), state)),
        ),
        patch.object(ingestion, "async_add_external_statistics") as write,
    ):
        await ingestion._import_open_day_locked(
            hass,
            coordinator,
            "site-123",
            now,
            {"commodities": {}},
        )

    write.assert_not_called()


@pytest.mark.asyncio
async def test_open_day_can_price_without_consumption_resource(hass) -> None:
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UK_TZ)
    state = {
        "_ingestion_version": ingestion.COST_INGESTION_SCHEMA_VERSION,
        "commodities": {
            "electricity": {
                "last_completed_day": "2026-09-07",
                "completed_total_sum_gbp": 2.0,
                "usage_sum_gbp": 1.0,
                "standing_sum_gbp": 1.0,
            }
        },
    }
    coordinator = _coordinator()
    coordinator.resources.pop(CLASSIFIER_ELECTRICITY_CONSUMPTION)
    coordinator.cost_breakdowns = {
        "electricity": _breakdown("2026-09-08", complete=False)
    }
    open_consumption = AsyncMock()

    with (
        patch.object(
            ingestion,
            "_load_store",
            new=AsyncMock(return_value=(FakeStore(state), state)),
        ),
        patch.object(ingestion, "_open_day_consumption", new=open_consumption),
        patch.object(ingestion, "async_add_external_statistics") as write,
    ):
        await ingestion._import_open_day_locked(
            hass,
            coordinator,
            "site-123",
            now,
            {"commodities": {}},
        )

    open_consumption.assert_not_awaited()
    assert write.call_count >= 1


@pytest.mark.asyncio
async def test_sync_loads_tariff_ledger_and_skips_open_day_when_reconcile_not_ready(hass) -> None:
    coordinator = _coordinator()
    ledger = {"commodities": {}}
    load = AsyncMock(return_value=ledger)
    reconcile = AsyncMock(return_value=False)
    open_day = AsyncMock()

    with (
        patch.object(ingestion, "_load_tariff_ledger", new=load),
        patch.object(ingestion, "_reconcile_locked", new=reconcile),
        patch.object(ingestion, "_import_open_day_locked", new=open_day),
    ):
        await ingestion.async_sync_cost_ingestion(hass, coordinator, "site-123")

    load.assert_awaited_once_with(hass, "site-123")
    reconcile.assert_awaited_once()
    open_day.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_day_consumption_fetches_midnight_to_now() -> None:
    now = datetime(2026, 9, 8, 12, 34, tzinfo=UK_TZ)
    fetch = AsyncMock(return_value=None)
    coordinator = SimpleNamespace(api_client=SimpleNamespace(_fetch_day_reading=fetch))

    assert await ingestion._open_day_consumption(coordinator, "resource", now) is None
    args = fetch.await_args.args
    assert args[0] == "resource"
    assert args[1].hour == 0
    assert args[1].minute == 0
    assert args[2] == now


@pytest.mark.asyncio
async def test_worker_runs_initial_refresh_and_sync_then_propagates_cancellation(hass) -> None:
    coordinator = _coordinator()
    ledger = {"commodities": {}}
    refresh = AsyncMock(return_value=ledger)
    sync = AsyncMock()
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])

    with (
        patch.object(ingestion.asyncio, "sleep", new=sleep),
        patch.object(ingestion, "_refresh_tariff_ledger", new=refresh),
        patch.object(ingestion, "async_sync_cost_ingestion", new=sync),
    ):
        with pytest.raises(asyncio.CancelledError):
            await ingestion.async_cost_ingestion_worker(
                hass,
                coordinator,
                "site-123",
            )

    refresh.assert_awaited_once_with(hass, coordinator, "site-123")
    sync.assert_awaited_once_with(
        hass,
        coordinator,
        "site-123",
        tariff_ledger=ledger,
    )


@pytest.mark.asyncio
async def test_worker_survives_initial_refresh_and_sync_errors(hass) -> None:
    coordinator = _coordinator()
    refresh = AsyncMock(side_effect=RuntimeError("tariff failed"))
    sync = AsyncMock(side_effect=RuntimeError("sync failed"))
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])

    with (
        patch.object(ingestion.asyncio, "sleep", new=sleep),
        patch.object(ingestion, "_refresh_tariff_ledger", new=refresh),
        patch.object(ingestion, "async_sync_cost_ingestion", new=sync),
    ):
        with pytest.raises(asyncio.CancelledError):
            await ingestion.async_cost_ingestion_worker(
                hass,
                coordinator,
                "site-123",
            )

    refresh.assert_awaited_once()
    sync.assert_awaited_once_with(
        hass,
        coordinator,
        "site-123",
        tariff_ledger=None,
    )
