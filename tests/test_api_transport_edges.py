from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientConnectionError, ClientResponseError

from custom_components.hildebrand_glow import api
from custom_components.hildebrand_glow.api import (
    UK_TZ,
    GlowmarktApiClient,
    GlowmarktApiError,
    GlowmarktAuthError,
)


class FakeResponse:
    def __init__(self, payload: Any = None, *, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.headers: dict[str, str] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def raise_for_status(self) -> None:
        return None

    async def json(self):
        return self.payload


class SequenceSession:
    def __init__(self, *, gets=None, posts=None) -> None:
        self.gets = list(gets or [])
        self.posts = list(posts or [])

    def _next(self, values):
        item = values.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, _url, **_kwargs):
        return self._next(self.gets)

    def post(self, _url, **_kwargs):
        return self._next(self.posts)


def _client(session) -> GlowmarktApiClient:
    client = GlowmarktApiClient("user", "password", session)
    client._token = "token"
    client._token_expiry = datetime.now() + timedelta(days=1)
    return client


@pytest.fixture(autouse=True)
def no_transport_delay(monkeypatch) -> None:
    monkeypatch.setattr(api, "API_MIN_REQUEST_SPACING_SECONDS", 0.0)
    monkeypatch.setattr(api, "API_MAX_BACKOFF_SECONDS", 0.0)


@pytest.mark.asyncio
async def test_request_slot_waits_when_lane_is_in_future(monkeypatch) -> None:
    client = _client(SequenceSession())
    loop = asyncio.get_running_loop()
    client._next_request_at = loop.time() + 1.0
    sleep = AsyncMock()
    monkeypatch.setattr(api.asyncio, "sleep", sleep)

    await client._wait_for_request_slot()

    sleep.assert_awaited_once()
    assert sleep.await_args.args[0] > 0


@pytest.mark.asyncio
async def test_authenticate_maps_non_401_http_error() -> None:
    client = GlowmarktApiClient(
        "user",
        "password",
        SequenceSession(posts=[FakeResponse(status=403)]),
    )

    with pytest.raises(GlowmarktAuthError, match="HTTP 403"):
        await client.authenticate()


@pytest.mark.asyncio
async def test_authenticate_exhausts_transient_status(monkeypatch) -> None:
    monkeypatch.setattr(api, "API_MAX_RETRIES", 0)
    client = GlowmarktApiClient(
        "user",
        "password",
        SequenceSession(posts=[FakeResponse(status=503)]),
    )

    with pytest.raises(GlowmarktApiError, match="temporarily unavailable"):
        await client.authenticate()


@pytest.mark.asyncio
async def test_authenticate_retries_client_error_and_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(api, "API_MAX_RETRIES", 1)
    client = GlowmarktApiClient(
        "user",
        "password",
        SequenceSession(
            posts=[
                ClientConnectionError("dropped"),
                FakeResponse({"valid": True, "token": "fresh"}),
            ]
        ),
    )

    assert await client.authenticate() is True
    assert client._token == "fresh"


@pytest.mark.asyncio
async def test_authenticate_client_error_exhaustion(monkeypatch) -> None:
    monkeypatch.setattr(api, "API_MAX_RETRIES", 0)
    client = GlowmarktApiClient(
        "user",
        "password",
        SequenceSession(posts=[ClientConnectionError("dropped")]),
    )

    with pytest.raises(GlowmarktApiError, match="connection failed after retries"):
        await client.authenticate()


@pytest.mark.asyncio
async def test_get_json_maps_response_and_connection_errors(monkeypatch) -> None:
    response_error = ClientResponseError(
        request_info=None,  # type: ignore[arg-type]
        history=(),
        status=418,
        message="teapot",
    )
    response_client = _client(SequenceSession(gets=[response_error]))
    with pytest.raises(GlowmarktApiError, match="HTTP 418"):
        await response_client._get_json("https://example.test")

    monkeypatch.setattr(api, "API_MAX_RETRIES", 1)
    retry_client = _client(
        SequenceSession(
            gets=[ClientConnectionError("dropped"), FakeResponse({"ok": True})]
        )
    )
    assert await retry_client._get_json("https://example.test") == {"ok": True}

    monkeypatch.setattr(api, "API_MAX_RETRIES", 0)
    exhausted = _client(SequenceSession(gets=[ClientConnectionError("dropped")]))
    with pytest.raises(GlowmarktApiError, match="connection failed after retries"):
        await exhausted._get_json("https://example.test")


@pytest.mark.asyncio
async def test_discover_resources_skips_non_dict_resource_payload() -> None:
    client = _client(SequenceSession())
    client._get_json = AsyncMock(return_value=["not", "a", "dict"])  # type: ignore[method-assign]

    assert await client.discover_resources("site-1") == {}


@pytest.mark.asyncio
async def test_fetch_history_chunk_filters_invalid_outside_and_null_rows() -> None:
    start = datetime(2026, 9, 1, tzinfo=UK_TZ)
    end = start + timedelta(days=2)
    inside1 = start + timedelta(hours=1)
    inside2 = start + timedelta(days=1, hours=2)
    client = _client(SequenceSession())
    client._request_readings = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            [int((start - timedelta(minutes=30)).timestamp()), 99.0],
            [int(inside2.timestamp()), 2.0],
            [int(inside1.timestamp()), 1.0],
            [int((start + timedelta(hours=3)).timestamp()), None],
            [int(end.timestamp()), 99.0],
            [123],
        ]
    )

    readings = await client._fetch_history_chunk("resource", start, end)

    assert [item.day for item in readings] == ["2026-09-01", "2026-09-02"]
    assert readings[0].value == 1.0
    assert readings[1].value == 2.0


@pytest.mark.asyncio
async def test_find_data_start_normalises_to_uk_midnight() -> None:
    client = _client(SequenceSession())
    client.get_first_available_reading_time = AsyncMock(  # type: ignore[method-assign]
        return_value=datetime(2026, 9, 1, 23, 30, tzinfo=timezone.utc)
    )

    result = await client._find_data_start("resource")

    assert result == datetime(2026, 9, 2, 0, 0, tzinfo=UK_TZ)


@pytest.mark.asyncio
async def test_get_all_wrapper_available_discovery_and_resources_property() -> None:
    client = _client(SequenceSession())
    client._resources = {"electricity.consumption": {"resource_id": "electricity"}}
    client.get_daily_reading = AsyncMock(return_value=None)  # type: ignore[method-assign]

    assert await client.get_all_readings() == {"electricity.consumption": None}
    assert client.resources == client._resources

    client._resources = {}
    client.discover_resources = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda _ve=None: client._resources.update(
            {"gas.consumption": {"resource_id": "gas"}}
        )
        or client._resources
    )
    client.get_available_daily_readings = AsyncMock(return_value=[])  # type: ignore[method-assign]

    assert await client.get_available_readings() == {"gas.consumption": []}
    client.discover_resources.assert_awaited_once()
