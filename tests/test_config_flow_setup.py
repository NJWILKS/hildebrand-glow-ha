from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType

from custom_components.hildebrand_glow.api import GlowmarktApiError, GlowmarktAuthError
from custom_components.hildebrand_glow.const import (
    CONF_CONSUMPTION_INTERVAL,
    CONF_COST_INTERVAL,
    CONF_ELECTRICITY_RATE,
    CONF_ELECTRICITY_STANDING_CHARGE,
    CONF_GAS_RATE,
    CONF_GAS_STANDING_CHARGE,
    CONF_VIRTUAL_ENTITY,
    DEFAULT_CONSUMPTION_INTERVAL,
    DEFAULT_COST_INTERVAL,
    DOMAIN,
)


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url: str) -> None:
    """Prepare Recorder before Home Assistant loads integration dependencies."""
    assert recorder_db_url


def _client(
    *,
    virtual_entities: list[dict] | None = None,
    resources: dict | None = None,
) -> AsyncMock:
    client = AsyncMock()
    client.authenticate.return_value = True
    client.get_virtual_entities.return_value = (
        virtual_entities
        if virtual_entities is not None
        else [{"veId": "site-1", "name": "Home"}]
    )
    client.discover_resources.return_value = (
        resources
        if resources is not None
        else {
            "electricity.consumption": {
                "resource_id": "electricity-resource",
            }
        }
    )
    return client


async def _start_user_flow(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )


@pytest.mark.asyncio
async def test_user_flow_initial_form(recorder_mock, hass) -> None:
    result = await _start_user_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    fields = {marker.schema for marker in result["data_schema"].schema}
    assert fields == {CONF_USERNAME, CONF_PASSWORD}


@pytest.mark.asyncio
async def test_user_flow_happy_path_creates_selected_site(recorder_mock, hass) -> None:
    client = _client()
    tariff = {
        CONF_ELECTRICITY_RATE: 0.245,
        CONF_ELECTRICITY_STANDING_CHARGE: 0.582,
        CONF_GAS_RATE: 0.065,
        CONF_GAS_STANDING_CHARGE: 0.31,
    }

    with patch(
        "custom_components.hildebrand_glow.config_flow.GlowmarktApiClient",
        return_value=client,
    ):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "select_entity"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_VIRTUAL_ENTITY: "site-1"},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "tariff"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=tariff,
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["data"] == {
        CONF_USERNAME: "user@example.com",
        CONF_PASSWORD: "secret",
        CONF_VIRTUAL_ENTITY: "site-1",
        **tariff,
        CONF_CONSUMPTION_INTERVAL: DEFAULT_CONSUMPTION_INTERVAL,
        CONF_COST_INTERVAL: DEFAULT_COST_INTERVAL,
    }
    client.authenticate.assert_awaited_once()
    client.get_virtual_entities.assert_awaited_once()
    client.discover_resources.assert_awaited_once_with("site-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (GlowmarktAuthError("bad credentials"), "invalid_auth"),
        (GlowmarktApiError("offline"), "cannot_connect"),
        (RuntimeError("unexpected"), "unknown"),
    ],
)
async def test_user_flow_maps_auth_and_connection_errors(
    recorder_mock,
    hass,
    failure: Exception,
    error: str,
) -> None:
    client = _client()
    client.authenticate.side_effect = failure

    with patch(
        "custom_components.hildebrand_glow.config_flow.GlowmarktApiClient",
        return_value=client,
    ):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}


@pytest.mark.asyncio
async def test_user_flow_rejects_account_without_virtual_entities(recorder_mock, hass) -> None:
    client = _client(virtual_entities=[])

    with patch(
        "custom_components.hildebrand_glow.config_flow.GlowmarktApiClient",
        return_value=client,
    ):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "no_resources"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resources", "failure", "error"),
    [
        ({}, None, "no_resources"),
        (None, GlowmarktApiError("offline"), "cannot_connect"),
    ],
)
async def test_select_entity_reports_resource_discovery_failures(
    recorder_mock,
    hass,
    resources: dict | None,
    failure: Exception | None,
    error: str,
) -> None:
    client = _client(resources=resources if resources is not None else {})
    if failure is not None:
        client.discover_resources.side_effect = failure

    with patch(
        "custom_components.hildebrand_glow.config_flow.GlowmarktApiClient",
        return_value=client,
    ):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_VIRTUAL_ENTITY: "site-1"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "select_entity"
    assert result["errors"] == {"base": error}


@pytest.mark.asyncio
async def test_select_entity_omits_entries_without_ids_and_uses_default_label(
    recorder_mock,
    hass,
) -> None:
    client = _client(
        virtual_entities=[
            {"name": "Missing ID"},
            {"veId": "site-1"},
        ]
    )

    with patch(
        "custom_components.hildebrand_glow.config_flow.GlowmarktApiClient",
        return_value=client,
    ):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"},
        )

    selector = next(iter(result["data_schema"].schema.values()))
    options = selector.config["options"]
    assert options == [{"value": "site-1", "label": "Unknown Location"}]
