from __future__ import annotations

import os

import aiohttp
import pytest

from custom_components.hildebrand_glow.api import GlowmarktApiClient

pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_live_account_authenticates_and_exposes_electricity() -> None:
    username = os.environ["GLOWMARKT_USERNAME"]
    password = os.environ["GLOWMARKT_PASSWORD"]

    async with aiohttp.ClientSession() as session:
        client = GlowmarktApiClient(username, password, session)
        assert await client.authenticate()
        virtual_entities = await client.get_virtual_entities()
        assert virtual_entities

        resources = await client.discover_resources()
        assert "electricity.consumption" in resources
