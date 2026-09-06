from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
import pytest

from custom_components.hildebrand_glow.api import UK_TZ, GlowmarktApiClient
from custom_components.hildebrand_glow.const import CLASSIFIER_ELECTRICITY_CONSUMPTION

pytestmark = pytest.mark.live

ORACLE = Path(__file__).parents[1] / "fixtures" / "electricity_export_oracle.json"
IMMUTABLE_TEST_DAYS = (
    "2025-07-30",
    "2025-10-26",
    "2026-03-29",
    "2026-09-05",
)


def _oracle() -> dict:
    return json.loads(ORACLE.read_text(encoding="utf-8"))


def _canonical_digest(rows: list[list[object]]) -> str:
    canonical = "\n".join(
        f"{int(row[0])}:{float(row[1]):.3f}"
        for row in rows
        if len(row) > 1 and row[1] is not None
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _known_electricity_resource(
    client: GlowmarktApiClient,
    expected_first_epoch: int,
) -> str:
    virtual_entities = await client.get_virtual_entities()
    if not virtual_entities:
        pytest.fail("Live contract: no Bright meter sites were returned")

    for virtual_entity in virtual_entities:
        virtual_entity_id = virtual_entity.get("veId")
        if not virtual_entity_id:
            continue
        resources = await client.discover_resources(virtual_entity_id)
        electricity = resources.get(CLASSIFIER_ELECTRICITY_CONSUMPTION)
        if not electricity:
            continue
        resource_id = electricity["resource_id"]
        first = await client.get_first_reading_time(resource_id)
        if first is not None and int(first.timestamp()) == expected_first_epoch:
            return resource_id

    pytest.fail("Live contract: known electricity history boundary was not found")


@pytest.mark.asyncio
async def test_live_account_matches_known_electricity_export() -> None:
    oracle = _oracle()
    username = os.environ["GLOWMARKT_USERNAME"]
    password = os.environ["GLOWMARKT_PASSWORD"]

    async with aiohttp.ClientSession() as session:
        client = GlowmarktApiClient(username, password, session)
        if not await client.authenticate():
            pytest.fail("Live contract: authentication failed")

        resource_id = await _known_electricity_resource(
            client,
            oracle["first_epoch_utc"],
        )

        last = await client.get_last_reading_time(resource_id)
        if last is None or int(last.timestamp()) < oracle["last_epoch_utc"]:
            pytest.fail("Live contract: API last-time is earlier than the known export")

        for day in IMMUTABLE_TEST_DAYS:
            expected = oracle["known_days"][day]
            start = datetime.fromisoformat(day).replace(tzinfo=UK_TZ)
            end = start + timedelta(days=1)
            rows = await client._request_readings(
                resource_id,
                start,
                end,
                "PT30M",
            )
            valid = [row for row in rows if len(row) > 1 and row[1] is not None]

            if len(valid) != expected["intervals"]:
                pytest.fail(f"Live contract: interval count mismatch for case {day}")
            if round(sum(float(row[1]) for row in valid), 3) != expected["kwh"]:
                pytest.fail(f"Live contract: daily total mismatch for case {day}")
            if _canonical_digest(valid) != expected["sha256"]:
                pytest.fail(f"Live contract: interval fingerprint mismatch for case {day}")
