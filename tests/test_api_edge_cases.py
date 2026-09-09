from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.hildebrand_glow import api
from custom_components.hildebrand_glow.api import (
    UK_TZ,
    DailyReading,
    GlowmarktApiClient,
    GlowmarktApiError,
    GlowmarktAuthError,
)


class FakeResponse:
    def __init__(
        self,
        payload: Any,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.payload = payload
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> Any:
        return self.payload


class FakeSession:
    def __init__(
        self,
        *,
        gets: list[FakeResponse | Any] | None = None,
        posts: list[FakeResponse | Any] | None = None,
    ) -> None:
        self.gets = list(gets or [])
        self.posts = list(posts or [])
        self.get_calls: list[tuple[str, dict[str, Any]]] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.get_calls.append((url, kwargs))
        item = self.gets.pop(0)
        return item if isinstance(item, FakeResponse) else FakeResponse(item)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.post_calls.append((url, kwargs))
        item = self.posts.pop(0)
        return item if isinstance(item, FakeResponse) else FakeResponse(item)


@pytest.fixture(autouse=True)
def disable_waits(monkeypatch) -> None:
    monkeypatch.setattr(api, "API_MIN_REQUEST_SPACING_SECONDS", 0.0)
    monkeypatch.setattr(api, "API_MAX_BACKOFF_SECONDS", 0.0)


def _client(session: FakeSession | None = None) -> GlowmarktApiClient:
    client = GlowmarktApiClient(
        "user",
        "password",
        session or FakeSession(),  # type: ignore[arg-type]
    )
    client._token = "token"
    client._token_expiry = datetime.now() + timedelta(days=1)
    return client


def _ts(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp())


def test_retry_delay_handles_invalid_and_negative_retry_after(monkeypatch) -> None:
    monkeypatch.setattr(api, "API_MAX_BACKOFF_SECONDS", 30.0)

    assert GlowmarktApiClient._retry_delay(FakeResponse({}, headers={"Retry-After": "bad"}), 2) == 4.0
    assert GlowmarktApiClient._retry_delay(FakeResponse({}, headers={"Retry-After": "-10"}), 0) == 0.0
    assert GlowmarktApiClient._retry_delay(FakeResponse({}, headers={"Retry-After": "99"}), 0) == 30.0


@pytest.mark.asyncio
async def test_authenticate_rejects_bad_credentials_and_invalid_payload() -> None:
    bad = GlowmarktApiClient(
        "user",
        "password",
        FakeSession(posts=[FakeResponse({}, status=401)]),  # type: ignore[arg-type]
    )
    with pytest.raises(GlowmarktAuthError, match="Invalid username"):
        await bad.authenticate()

    malformed = GlowmarktApiClient(
        "user",
        "password",
        FakeSession(posts=[{"valid": False}]),  # type: ignore[arg-type]
    )
    with pytest.raises(GlowmarktAuthError, match="invalid response"):
        await malformed.authenticate()


@pytest.mark.asyncio
async def test_authenticate_retries_transient_then_succeeds() -> None:
    session = FakeSession(
        posts=[
            FakeResponse({}, status=503, headers={"Retry-After": "0"}),
            {"valid": True, "token": "new-token"},
        ]
    )
    client = GlowmarktApiClient("user", "password", session)  # type: ignore[arg-type]

    assert await client.authenticate() is True
    assert client._token == "new-token"
    assert len(session.post_calls) == 2


@pytest.mark.asyncio
async def test_ensure_authenticated_refreshes_missing_or_expired_token() -> None:
    client = _client()
    client.authenticate = AsyncMock(return_value=True)  # type: ignore[method-assign]

    client._token = None
    await client._ensure_authenticated()
    client.authenticate.assert_awaited_once()

    client.authenticate.reset_mock()
    client._token = "old"
    client._token_expiry = datetime.now() - timedelta(seconds=1)
    await client._ensure_authenticated()
    client.authenticate.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_json_allows_not_found_and_exhausts_transient_retries(monkeypatch) -> None:
    not_found = _client(FakeSession(gets=[FakeResponse({}, status=404)]))
    assert await not_found._get_json("https://example.test", allow_not_found=True) is None

    monkeypatch.setattr(api, "API_MAX_RETRIES", 1)
    exhausted = _client(
        FakeSession(
            gets=[
                FakeResponse({}, status=503, headers={"Retry-After": "0"}),
                FakeResponse({}, status=503, headers={"Retry-After": "0"}),
            ]
        )
    )
    with pytest.raises(GlowmarktApiError, match="failed after retries"):
        await exhausted._get_json("https://example.test")


@pytest.mark.asyncio
async def test_virtual_entities_and_resource_discovery_ignore_malformed_payloads() -> None:
    client = _client()
    client._get_json = AsyncMock(return_value={"not": "a list"})  # type: ignore[method-assign]
    assert await client.get_virtual_entities() == []

    client.get_virtual_entities = AsyncMock(return_value=[])  # type: ignore[method-assign]
    assert await client.discover_resources() == {}

    client = _client()
    client._get_json = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "resources": [
                    {"resourceId": "missing-classifier"},
                    {"classifier": "missing.resource.id"},
                    {
                        "resourceId": "ok",
                        "classifier": "electricity.consumption",
                    },
                ]
            }
        ]
    )
    resources = await client.discover_resources("site-1")
    assert resources == {
        "electricity.consumption": {
            "resource_id": "ok",
            "name": "electricity.consumption",
            "classifier": "electricity.consumption",
            "base_unit": "",
        }
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("method,key", [("get_first_reading_time", "firstTs"), ("get_last_reading_time", "lastTs")])
async def test_first_and_last_time_handle_missing_and_error_payloads(method: str, key: str) -> None:
    client = _client()
    target = getattr(client, method)

    client._get_json = AsyncMock(return_value=None)  # type: ignore[method-assign]
    assert await target("resource") is None

    client._get_json = AsyncMock(return_value={"status": "OK", "data": {}})  # type: ignore[method-assign]
    assert await target("resource") is None

    client._get_json = AsyncMock(return_value={"status": "ERROR", "data": {key: 1}})  # type: ignore[method-assign]
    with pytest.raises(GlowmarktApiError):
        await target("resource")


@pytest.mark.asyncio
async def test_request_readings_rejects_bad_status_and_non_list_data() -> None:
    client = _client()
    client._get_json = AsyncMock(return_value={"status": "ERROR"})  # type: ignore[method-assign]
    with pytest.raises(GlowmarktApiError, match="readings query"):
        await client._request_readings(
            "resource",
            datetime(2026, 9, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 2, tzinfo=timezone.utc),
            "PT30M",
        )

    client._get_json = AsyncMock(return_value={"status": "OK", "data": {}})  # type: ignore[method-assign]
    assert await client._request_readings(
        "resource",
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 2, tzinfo=timezone.utc),
        "PT30M",
    ) == []


@pytest.mark.asyncio
async def test_first_available_reading_returns_none_without_locator() -> None:
    client = _client()
    client.get_first_reading_time = AsyncMock(return_value=None)  # type: ignore[method-assign]

    assert await client.get_first_available_reading_time("resource") is None


@pytest.mark.asyncio
async def test_fetch_day_reading_filters_invalid_and_boundary_rows() -> None:
    day_start = datetime(2026, 9, 5, tzinfo=UK_TZ)
    day_end = day_start + timedelta(days=1)
    client = _client()
    client._request_readings = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            [_ts("2026-09-04T22:00:00+00:00"), 99.0],
            [_ts("2026-09-04T23:00:00+00:00"), 0.2],
            [_ts("2026-09-04T23:30:00+00:00"), None],
            [_ts("2026-09-05T23:00:00+00:00"), 99.0],
            [123],
        ]
    )

    reading = await client._fetch_day_reading("resource", day_start, day_end)

    assert reading is not None
    assert reading.day == "2026-09-05"
    assert reading.value == 0.2
    assert len(reading.intervals) == 1


@pytest.mark.asyncio
async def test_history_and_selected_reading_helpers_cover_empty_and_filtered_resources() -> None:
    client = _client()
    client._find_data_start = AsyncMock(return_value=None)  # type: ignore[method-assign]
    assert await client.get_available_daily_readings("resource") == []

    client._resources = {
        "electricity.consumption": {"resource_id": "electricity"},
        "gas.consumption": {"resource_id": "gas"},
    }
    client.get_daily_reading = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda resource_id: DailyReading(
            day="2026-09-01",
            value=1.0 if resource_id == "electricity" else 2.0,
            intervals=[],
        )
    )
    readings = await client.get_readings({"electricity.consumption"})
    assert set(readings) == {"electricity.consumption"}

    client.get_available_daily_readings = AsyncMock(  # type: ignore[method-assign]
        return_value=[]
    )
    available = await client.get_available_readings({"gas.consumption"})
    assert available == {"gas.consumption": []}


@pytest.mark.asyncio
async def test_reading_helpers_discover_resources_when_cache_is_empty() -> None:
    client = _client()
    client.discover_resources = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda _ve=None: client._resources.update(
            {"electricity.consumption": {"resource_id": "electricity"}}
        )
        or client._resources
    )
    client.get_daily_reading = AsyncMock(return_value=None)  # type: ignore[method-assign]

    assert await client.get_readings() == {"electricity.consumption": None}
    client.discover_resources.assert_awaited_once()


@pytest.mark.asyncio
async def test_connection_reports_success_and_known_failures() -> None:
    client = _client()
    client.authenticate = AsyncMock(return_value=True)  # type: ignore[method-assign]
    client.discover_resources = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda: client._resources.update(
            {"electricity.consumption": {"resource_id": "resource"}}
        )
        or client._resources
    )
    assert await client.test_connection() is True

    client = _client()
    client.authenticate = AsyncMock(side_effect=GlowmarktAuthError("bad"))  # type: ignore[method-assign]
    assert await client.test_connection() is False

    client = _client()
    client.authenticate = AsyncMock(return_value=True)  # type: ignore[method-assign]
    client.discover_resources = AsyncMock(side_effect=GlowmarktApiError("down"))  # type: ignore[method-assign]
    assert await client.test_connection() is False
