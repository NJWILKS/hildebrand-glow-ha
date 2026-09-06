from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.hildebrand_glow.api import (
    UK_TZ,
    GlowmarktApiClient,
    GlowmarktApiError,
)


class FakeResponse:
    def __init__(
        self,
        payload: Any,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> Any:
        return self._payload


class FakeSession:
    def __init__(self, get_payloads: list[Any]) -> None:
        self._get_payloads = list(get_payloads)
        self.get_calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.get_calls.append((url, kwargs))
        item = self._get_payloads.pop(0)
        return item if isinstance(item, FakeResponse) else FakeResponse(item)


@pytest.fixture(autouse=True)
def disable_test_request_pacing(monkeypatch) -> None:
    monkeypatch.setattr(
        "custom_components.hildebrand_glow.api.API_MIN_REQUEST_SPACING_SECONDS",
        0.0,
    )


def _authenticated_client(session: FakeSession) -> GlowmarktApiClient:
    client = GlowmarktApiClient("user", "password", session)  # type: ignore[arg-type]
    client._token = "test-token"
    client._token_expiry = datetime.now() + timedelta(days=1)
    return client


def _uk_epoch(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
) -> int:
    value = datetime(year, month, day, hour, minute, tzinfo=UK_TZ)
    return int(value.astimezone(timezone.utc).timestamp())


@pytest.mark.asyncio
async def test_daily_reading_returns_complete_day_with_intervals(freezer) -> None:
    freezer.move_to("2026-09-06 12:00:00")
    start = int(datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc).timestamp())
    session = FakeSession(
        [
            {
                "status": "OK",
                "data": [
                    [start, 0.101],
                    [start + 1800, 0.202],
                    [start + 3600, None],
                    [start + 5400, 0.303],
                ],
            }
        ]
    )
    client = _authenticated_client(session)

    result = await client.get_daily_reading("electricity-resource")

    assert result is not None
    assert result.day == "2026-09-05"
    assert result.value == 0.606
    assert len(result.intervals) == 3
    params = session.get_calls[0][1]["params"]
    assert params["period"] == "PT30M"
    assert params["function"] == "sum"
    assert params["nulls"] == 1


@pytest.mark.asyncio
async def test_daily_reading_preserves_genuine_zero_day(freezer) -> None:
    freezer.move_to("2026-09-06 12:00:00")
    start = int(datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc).timestamp())
    session = FakeSession(
        [{"status": "OK", "data": [[start, 0.0], [start + 1800, 0.0]]}]
    )
    client = _authenticated_client(session)

    result = await client.get_daily_reading("electricity-resource")

    assert result is not None
    assert result.day == "2026-09-05"
    assert result.value == 0.0
    assert len(session.get_calls) == 1


@pytest.mark.asyncio
async def test_daily_reading_falls_back_when_latest_day_is_missing(freezer) -> None:
    freezer.move_to("2026-09-06 12:00:00")
    prior = int(datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc).timestamp())
    session = FakeSession(
        [
            {"status": "OK", "data": [[prior + 86400, None]]},
            {"status": "OK", "data": [[prior, 0.4], [prior + 1800, 0.6]]},
        ]
    )
    client = _authenticated_client(session)

    result = await client.get_daily_reading("electricity-resource")

    assert result is not None
    assert result.day == "2026-09-04"
    assert result.value == 1.0
    assert len(session.get_calls) == 2


@pytest.mark.asyncio
async def test_daily_reading_returns_none_after_three_empty_days(freezer) -> None:
    freezer.move_to("2026-09-06 12:00:00")
    session = FakeSession(
        [
            {"status": "OK", "data": []},
            {"status": "OK", "data": [[1_700_000_000, None]]},
            {"status": "OK", "data": []},
        ]
    )
    client = _authenticated_client(session)

    assert await client.get_daily_reading("electricity-resource") is None
    assert len(session.get_calls) == 3


@pytest.mark.asyncio
async def test_first_reading_time_uses_authoritative_endpoint() -> None:
    session = FakeSession(
        [{"status": "OK", "data": {"firstTs": 1_753_830_000}}]
    )
    client = _authenticated_client(session)

    first = await client.get_first_reading_time("electricity-resource")

    assert first == datetime(2025, 7, 29, 23, 0, tzinfo=timezone.utc)
    assert len(session.get_calls) == 1
    assert session.get_calls[0][0].endswith(
        "/resource/electricity-resource/first-time"
    )


@pytest.mark.asyncio
async def test_last_reading_time_uses_authoritative_endpoint() -> None:
    session = FakeSession(
        [{"status": "OK", "data": {"lastTs": 1_788_651_000}}]
    )
    client = _authenticated_client(session)

    last = await client.get_last_reading_time("electricity-resource")

    assert last == datetime(2026, 9, 5, 23, 30, tzinfo=timezone.utc)
    assert len(session.get_calls) == 1
    assert session.get_calls[0][0].endswith(
        "/resource/electricity-resource/last-time"
    )


@pytest.mark.asyncio
async def test_history_discovery_uses_first_time_not_year_scanning() -> None:
    session = FakeSession(
        [
            {
                "status": "OK",
                "data": {
                    "firstTs": int(
                        datetime(2018, 6, 15, 12, 30, tzinfo=timezone.utc).timestamp()
                    )
                },
            }
        ]
    )
    client = _authenticated_client(session)

    start = await client._find_data_start("electricity-resource")

    assert start is not None
    assert start.date().isoformat() == "2018-06-15"
    assert len(session.get_calls) == 1
    assert session.get_calls[0][0].endswith("/first-time")
    assert session.get_calls[0][1].get("params") is None


@pytest.mark.asyncio
async def test_full_history_uses_safe_multi_day_pt30m_chunks(freezer) -> None:
    freezer.move_to("2026-09-13 12:00:00")
    session = FakeSession(
        [
            {
                "status": "OK",
                "data": [
                    [_uk_epoch(2026, 9, 1), 0.1],
                    [_uk_epoch(2026, 9, 1, 0, 30), 0.2],
                    [_uk_epoch(2026, 9, 9, 23, 30), 0.3],
                ],
            },
            {
                "status": "OK",
                "data": [
                    [_uk_epoch(2026, 9, 10), 0.0],
                    [_uk_epoch(2026, 9, 10, 0, 30), 0.0],
                    [_uk_epoch(2026, 9, 12), 0.4],
                ],
            },
        ]
    )
    client = _authenticated_client(session)
    client._find_data_start = AsyncMock(  # type: ignore[method-assign]
        return_value=datetime(2026, 9, 1, tzinfo=UK_TZ)
    )

    readings = await client.get_available_daily_readings("electricity-resource")

    assert [reading.day for reading in readings] == [
        "2026-09-01",
        "2026-09-09",
        "2026-09-10",
        "2026-09-12",
    ]
    assert readings[0].value == 0.3
    assert readings[2].value == 0.0
    assert len(session.get_calls) == 2
    assert all(
        call[1]["params"]["period"] == "PT30M"
        for call in session.get_calls
    )

    first_from = datetime.fromisoformat(session.get_calls[0][1]["params"]["from"])
    first_to = datetime.fromisoformat(session.get_calls[0][1]["params"]["to"])
    assert first_to - first_from == timedelta(days=9)


@pytest.mark.asyncio
async def test_first_time_api_error_is_not_treated_as_end_of_history() -> None:
    session = FakeSession([{"status": "ERROR", "data": {}}])
    client = _authenticated_client(session)

    with pytest.raises(GlowmarktApiError):
        await client._find_data_start("electricity-resource")


@pytest.mark.asyncio
async def test_rate_limit_retries_without_returning_fake_empty_data() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {"status": "ERROR"},
                status=429,
                headers={"Retry-After": "0"},
            ),
            {"status": "OK", "data": [[1_700_000_000, 0.5]]},
        ]
    )
    client = _authenticated_client(session)

    rows = await client._request_readings(
        "electricity-resource",
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 2, tzinfo=timezone.utc),
        "PT30M",
    )

    assert rows == [[1_700_000_000, 0.5]]
    assert len(session.get_calls) == 2


@pytest.mark.asyncio
async def test_discover_resources_for_single_virtual_entity() -> None:
    session = FakeSession(
        [
            [{"veId": "site-1"}],
            {
                "resources": [
                    {
                        "resourceId": "electricity-resource",
                        "classifier": "electricity.consumption",
                        "name": "Electricity consumption",
                        "baseUnit": "kWh",
                    }
                ]
            },
        ]
    )
    client = _authenticated_client(session)

    resources = await client.discover_resources()

    assert resources["electricity.consumption"]["resource_id"] == (
        "electricity-resource"
    )
    assert resources["electricity.consumption"]["base_unit"] == "kWh"


@pytest.mark.asyncio
async def test_discover_resources_can_target_selected_virtual_entity() -> None:
    session = FakeSession(
        [
            {
                "resources": [
                    {
                        "resourceId": "site-2-electricity",
                        "classifier": "electricity.consumption",
                        "name": "Electricity consumption",
                        "baseUnit": "kWh",
                    }
                ]
            }
        ]
    )
    client = _authenticated_client(session)

    resources = await client.discover_resources("site-2")

    assert len(session.get_calls) == 1
    assert "/virtualentity/site-2/resources" in session.get_calls[0][0]
    assert resources["electricity.consumption"]["resource_id"] == (
        "site-2-electricity"
    )
