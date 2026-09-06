from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from custom_components.hildebrand_glow.api import GlowmarktApiClient


class FakeResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status = status

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
        return FakeResponse(self._get_payloads.pop(0))


def _authenticated_client(session: FakeSession) -> GlowmarktApiClient:
    client = GlowmarktApiClient("user", "password", session)  # type: ignore[arg-type]
    client._token = "test-token"
    client._token_expiry = datetime.now() + timedelta(days=1)
    return client


@pytest.mark.asyncio
async def test_daily_reading_sums_half_hour_values() -> None:
    session = FakeSession(
        [
            {
                "status": "OK",
                "data": [
                    [1_700_000_000, 0.101],
                    [1_700_001_800, 0.202],
                    [1_700_003_600, None],
                    [1_700_005_400, 0.303],
                ],
            }
        ]
    )
    client = _authenticated_client(session)

    result = await client.get_daily_reading("electricity-resource")

    assert result == 0.606
    assert session.get_calls[0][1]["params"]["period"] == "PT30M"
    assert session.get_calls[0][1]["params"]["function"] == "sum"


@pytest.mark.asyncio
async def test_daily_reading_returns_none_when_api_has_no_data() -> None:
    session = FakeSession([{"status": "OK", "data": []}])
    client = _authenticated_client(session)

    assert await client.get_daily_reading("electricity-resource") is None


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

    assert resources["electricity.consumption"]["resource_id"] == "electricity-resource"
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
    assert resources["electricity.consumption"]["resource_id"] == "site-2-electricity"
