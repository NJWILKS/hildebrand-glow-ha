from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from aiohttp import ServerDisconnectedError

from custom_components.hildebrand_glow import api
from custom_components.hildebrand_glow.api import GlowmarktApiClient


class FakeResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status = status
        self.headers: dict[str, str] = {}

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def json(self) -> Any:
        return self._payload


class DisconnectOnceSession:
    def __init__(self) -> None:
        self.get_calls = 0
        self.post_calls = 0

    def get(self, _url: str, **_kwargs: Any) -> FakeResponse:
        self.get_calls += 1
        if self.get_calls == 1:
            raise ServerDisconnectedError()
        return FakeResponse({"status": "OK", "data": [[1_700_000_000, 0.5]]})

    def post(self, _url: str, **_kwargs: Any) -> FakeResponse:
        self.post_calls += 1
        if self.post_calls == 1:
            raise ServerDisconnectedError()
        return FakeResponse({"valid": True, "token": "test-token"})


@pytest.fixture(autouse=True)
def disable_retry_wait(monkeypatch) -> None:
    monkeypatch.setattr(api, "API_MIN_REQUEST_SPACING_SECONDS", 0.0)
    monkeypatch.setattr(api, "API_MAX_BACKOFF_SECONDS", 0.0)


@pytest.mark.asyncio
async def test_readings_retry_server_disconnect() -> None:
    session = DisconnectOnceSession()
    client = GlowmarktApiClient("user", "password", session)  # type: ignore[arg-type]
    client._token = "test-token"
    client._token_expiry = datetime.now() + timedelta(days=1)

    rows = await client._request_readings(
        "electricity-resource",
        datetime.fromisoformat("2026-09-01T00:00:00+00:00"),
        datetime.fromisoformat("2026-09-02T00:00:00+00:00"),
        "PT30M",
    )

    assert rows == [[1_700_000_000, 0.5]]
    assert session.get_calls == 2


@pytest.mark.asyncio
async def test_authentication_retries_server_disconnect() -> None:
    session = DisconnectOnceSession()
    client = GlowmarktApiClient("user", "password", session)  # type: ignore[arg-type]

    assert await client.authenticate() is True
    assert session.post_calls == 2
