from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_recorder_block_till_done,
)

from custom_components.hildebrand_glow.reset import _clear_statistics


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url: str) -> None:
    """Prepare the real test Recorder before Home Assistant starts."""
    assert recorder_db_url


@pytest.mark.asyncio
async def test_clear_statistics_returns_immediately_for_empty_set(recorder_mock, hass) -> None:
    await _clear_statistics(hass, [])


@pytest.mark.asyncio
async def test_clear_statistics_waits_for_recorder_callback(recorder_mock, hass) -> None:
    """Exercise the callback path that previously blocked config-entry bootstrap."""
    await _clear_statistics(hass, ["hildebrand_glow:nonexistent_statistic"])
    await async_recorder_block_till_done(hass)
