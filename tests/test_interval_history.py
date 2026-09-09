from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import pytest

from custom_components.hildebrand_glow import interval_history as ih
from custom_components.hildebrand_glow.api import GlowmarktApiError
from custom_components.hildebrand_glow.const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_ELECTRICITY_COST,
)


class FakeStore:
    data: dict[str, dict[str, Any]] = {}

    def __init__(self, hass, version: int, key: str) -> None:
        self.key = key

    async def async_load(self):
        value = self.data.get(self.key)
        return deepcopy(value) if value is not None else None

    async def async_save(self, value) -> None:
        self.data[self.key] = deepcopy(value)


class FakeApi:
    def __init__(
        self,
        *,
        first: dict[str, datetime | None],
        rows: dict[str, list[list[Any]]],
        tariff_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.first = first
        self.rows = rows
        self.tariff_rows = tariff_rows or []
        self.reading_calls: list[tuple[str, datetime, datetime, str]] = []
        self.first_calls: list[str] = []
        self.tariff_calls = 0

    async def get_first_available_reading_time(self, resource_id: str):
        self.first_calls.append(resource_id)
        return self.first.get(resource_id)

    async def _request_readings(
        self,
        resource_id: str,
        start: datetime,
        end: datetime,
        period: str,
    ):
        self.reading_calls.append((resource_id, start, end, period))
        return deepcopy(self.rows.get(resource_id, []))

    async def _get_json(self, url: str):
        self.tariff_calls += 1
        return {"status": "OK", "data": deepcopy(self.tariff_rows)}


def _ts(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


@pytest.fixture(autouse=True)
def fake_store(monkeypatch):
    FakeStore.data = {}
    monkeypatch.setattr(ih, "Store", FakeStore)


def _resources() -> dict[str, dict[str, Any]]:
    return {
        CLASSIFIER_ELECTRICITY_CONSUMPTION: {
            "resource_id": "electricity-usage",
            "base_unit": "kWh",
        },
        CLASSIFIER_ELECTRICITY_COST: {
            "resource_id": "electricity-cost",
            "base_unit": "p",
        },
    }


@pytest.mark.asyncio
async def test_population_stores_pt30m_usage_cost_and_tariff_reference() -> None:
    first = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    api = FakeApi(
        first={"electricity-usage": first, "electricity-cost": first},
        rows={
            "electricity-usage": [
                [_ts("2026-01-01T00:00:00+00:00"), 0.0],
                [_ts("2026-01-01T00:30:00+00:00"), 0.2],
                [_ts("2026-01-01T01:00:00+00:00"), 0.3],
                [_ts("2026-01-01T01:30:00+00:00"), None],
            ],
            "electricity-cost": [
                [_ts("2026-01-01T00:00:00+00:00"), 1.0],
                [_ts("2026-01-01T01:00:00+00:00"), 3.0],
            ],
        },
        tariff_rows=[
            {
                "effectiveDate": "2026-01-01",
                "plan": {"standing": 50.0, "rate": 25.0},
            }
        ],
    )

    metadata = await ih.async_populate_interval_history(
        object(),
        api,
        _resources(),
        "site",
        now_utc=datetime(2026, 1, 1, 1, 31, tzinfo=timezone.utc),
    )

    state = metadata["commodities"]["electricity"]
    assert state["first_interval"] == "2026-01-01T00:00:00+00:00"
    assert state["cursor_utc"] == "2026-01-01T01:30:00+00:00"
    assert state["status"] == "current"
    assert state["tariffs"]["periods"][0]["effective_from"] == "2026-01-01"
    assert state["tariffs"]["periods"][0]["unit_rate_pence_per_kwh"] == 25.0

    repository = ih.IntervalHistoryStore(object(), "site")
    intervals = await repository.async_load_month("electricity", "2026-01")
    assert list(intervals) == [
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:30:00+00:00",
        "2026-01-01T01:00:00+00:00",
    ]
    assert intervals["2026-01-01T00:00:00+00:00"]["usage_kwh"] == 0.0
    assert intervals["2026-01-01T00:30:00+00:00"]["cost_pence"] is None
    assert (
        intervals["2026-01-01T01:00:00+00:00"]["tariff_effective_from"]
        == "2026-01-01"
    )


@pytest.mark.asyncio
async def test_replayed_chunk_upserts_by_timestamp_without_duplicates() -> None:
    first = datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc)
    api = FakeApi(
        first={"electricity-usage": first, "electricity-cost": first},
        rows={
            "electricity-usage": [
                [_ts("2026-02-01T00:00:00+00:00"), 0.1],
                [_ts("2026-02-01T00:30:00+00:00"), 0.2],
            ],
            "electricity-cost": [
                [_ts("2026-02-01T00:00:00+00:00"), 1.0],
                [_ts("2026-02-01T00:30:00+00:00"), 2.0],
            ],
        },
    )
    now = datetime(2026, 2, 1, 1, 1, tzinfo=timezone.utc)

    await ih.async_populate_interval_history(object(), api, _resources(), "site", now_utc=now)

    metadata_store = FakeStore.data["hildebrand_glow_site_interval_history"]
    metadata_store["commodities"]["electricity"]["cursor_utc"] = (
        "2026-02-01T00:00:00+00:00"
    )
    FakeStore.data["hildebrand_glow_site_interval_history"] = deepcopy(metadata_store)

    await ih.async_populate_interval_history(object(), api, _resources(), "site", now_utc=now)

    repository = ih.IntervalHistoryStore(object(), "site")
    intervals = await repository.async_load_month("electricity", "2026-02")
    assert len(intervals) == 2
    assert intervals["2026-02-01T00:00:00+00:00"]["usage_kwh"] == 0.1
    assert intervals["2026-02-01T00:30:00+00:00"]["cost_pence"] == 2.0


@pytest.mark.asyncio
async def test_autumn_dst_duplicate_local_hour_keeps_distinct_utc_keys() -> None:
    first = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
    api = FakeApi(
        first={"electricity-usage": first, "electricity-cost": first},
        rows={
            "electricity-usage": [
                [_ts("2026-10-25T00:30:00+00:00"), 0.4],
                [_ts("2026-10-25T01:30:00+00:00"), 0.5],
            ],
            "electricity-cost": [
                [_ts("2026-10-25T00:30:00+00:00"), 4.0],
                [_ts("2026-10-25T01:30:00+00:00"), 5.0],
            ],
        },
        tariff_rows=[{"effectiveDate": "2026-10-01", "plan": {"rate": 20.0}}],
    )

    await ih.async_populate_interval_history(
        object(),
        api,
        _resources(),
        "site",
        now_utc=datetime(2026, 10, 25, 2, 1, tzinfo=timezone.utc),
    )

    repository = ih.IntervalHistoryStore(object(), "site")
    intervals = await repository.async_load_month("electricity", "2026-10")
    assert set(intervals) == {
        "2026-10-25T00:30:00+00:00",
        "2026-10-25T01:30:00+00:00",
    }


@pytest.mark.asyncio
async def test_first_interval_uses_earliest_real_usage_or_cost_reading() -> None:
    usage_first = datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)
    cost_first = datetime(2026, 3, 1, 23, 30, tzinfo=timezone.utc)
    api = FakeApi(
        first={"electricity-usage": usage_first, "electricity-cost": cost_first},
        rows={"electricity-usage": [], "electricity-cost": []},
    )

    metadata = await ih.async_populate_interval_history(
        object(),
        api,
        _resources(),
        "site",
        now_utc=datetime(2026, 3, 2, 0, 1, tzinfo=timezone.utc),
    )

    assert api.first_calls == ["electricity-usage", "electricity-cost"]
    assert (
        metadata["commodities"]["electricity"]["first_interval"]
        == "2026-03-01T23:30:00+00:00"
    )


@pytest.mark.asyncio
async def test_tariff_api_failure_propagates() -> None:
    class FailingTariffApi(FakeApi):
        async def _get_json(self, url: str):
            return {"status": "ERROR", "data": []}

    first = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
    api = FailingTariffApi(
        first={"electricity-usage": first, "electricity-cost": first},
        rows={},
    )

    with pytest.raises(GlowmarktApiError, match="tariff-list"):
        await ih.async_populate_interval_history(
            object(),
            api,
            _resources(),
            "site",
            now_utc=datetime(2026, 4, 1, 1, 0, tzinfo=timezone.utc),
        )
