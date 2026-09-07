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

ORACLE = (
    Path(__file__).parents[1]
    / "tests"
    / "fixtures"
    / "electricity_export_oracle.json"
)
LOCATOR_TOLERANCE = timedelta(days=3)
IMMUTABLE_TEST_DAYS = (
    "2025-10-26",
    "2026-03-29",
    "2026-09-05",
)


def _oracle() -> dict:
    return json.loads(ORACLE.read_text(encoding="utf-8"))


def _canonical_digest(intervals: list[tuple[datetime, float]]) -> str:
    canonical = "\n".join(
        f"{int(timestamp.timestamp())}:{value:.3f}"
        for timestamp, value in intervals
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _known_electricity_resource(
    client: GlowmarktApiClient,
    expected_first_epoch: int,
) -> tuple[str, datetime]:
    """Find the contributed electricity resource using first-time as a locator."""
    virtual_entities = await client.get_virtual_entities()
    if not virtual_entities:
        pytest.fail("Live contract: no Bright meter sites were returned")

    expected = datetime.fromtimestamp(expected_first_epoch, tz=UK_TZ)
    for virtual_entity in virtual_entities:
        virtual_entity_id = virtual_entity.get("veId")
        if not virtual_entity_id:
            continue
        resources = await client.discover_resources(virtual_entity_id)
        electricity = resources.get(CLASSIFIER_ELECTRICITY_CONSUMPTION)
        if not electricity:
            continue
        resource_id = electricity["resource_id"]
        locator = await client.get_first_reading_time(resource_id)
        if locator is not None and abs(locator - expected) <= LOCATOR_TOLERANCE:
            return resource_id, locator

    pytest.fail("Live contract: known electricity history locator was not found")


@pytest.mark.asyncio
async def test_live_account_matches_known_electricity_export() -> None:
    """Compare current billing readings with stable parts of the contributed export."""
    oracle = _oracle()
    username = os.environ["GLOWMARKT_USERNAME"]
    password = os.environ["GLOWMARKT_PASSWORD"]

    async with aiohttp.ClientSession() as session:
        client = GlowmarktApiClient(username, password, session)
        if not await client.authenticate():
            pytest.fail("Live contract: authentication failed")

        resource_id, locator = await _known_electricity_resource(
            client,
            oracle["first_epoch_utc"],
        )

        # first-time is a locator only. The readings endpoint is authoritative for
        # the first actual billable interval, because historical rows can be revised
        # or disappear independently of first-time metadata.
        first_actual = await client.get_first_available_reading_time(resource_id)
        if first_actual is None:
            pytest.fail("Live contract: no actual reading found near first-time locator")
        if abs(first_actual - locator) > LOCATOR_TOLERANCE:
            pytest.fail("Live contract: actual history starts too far from first-time locator")

        last = await client.get_last_reading_time(resource_id)
        if last is None or int(last.timestamp()) < oracle["last_epoch_utc"]:
            pytest.fail("Live contract: API last-time is earlier than the known export")

        # The first export day is deliberately not fingerprinted: current Glow
        # billing data no longer exposes its first two historical intervals even
        # though first-time still points there. Later stable days, including both
        # UK DST transitions, remain strict regression oracles.
        for day in IMMUTABLE_TEST_DAYS:
            expected = oracle["known_days"][day]
            start = datetime.fromisoformat(day).replace(tzinfo=UK_TZ)
            end = start + timedelta(days=1)
            reading = await client._fetch_day_reading(
                resource_id,
                start,
                end,
            )
            if reading is None:
                pytest.fail(f"Live contract: no completed-day data for case {day}")

            if len(reading.intervals) != expected["intervals"]:
                pytest.fail(f"Live contract: interval count mismatch for case {day}")
            if reading.value != expected["kwh"]:
                pytest.fail(f"Live contract: daily total mismatch for case {day}")
            if _canonical_digest(reading.intervals) != expected["sha256"]:
                pytest.fail(f"Live contract: interval fingerprint mismatch for case {day}")
