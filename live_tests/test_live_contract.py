from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import pytest

from custom_components.hildebrand_glow.api import UK_TZ, GlowmarktApiClient
from custom_components.hildebrand_glow.const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    GLOWMARKT_API_BASE,
)

pytestmark = pytest.mark.live

ORACLE = (
    Path(__file__).parents[1]
    / "tests"
    / "fixtures"
    / "electricity_export_oracle.json"
)
IMMUTABLE_TEST_DAYS = (
    "2025-07-30",
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


def _timestamp_geometry(
    start: datetime,
    end: datetime,
    intervals: list[tuple[datetime, float]],
) -> tuple[list[int], list[int]]:
    start_epoch = int(start.astimezone(timezone.utc).timestamp())
    end_epoch = int(end.astimezone(timezone.utc).timestamp())
    expected_epochs = set(range(start_epoch, end_epoch, 1800))
    actual_epochs = {int(timestamp.timestamp()) for timestamp, _ in intervals}
    return sorted(expected_epochs - actual_epochs), sorted(actual_epochs - expected_epochs)


def _filter_local_day(
    rows: list[list[object]],
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, float]]:
    result: list[tuple[datetime, float]] = []
    for row in rows:
        if len(row) <= 1 or row[1] is None:
            continue
        timestamp = datetime.fromtimestamp(float(row[0]), tz=timezone.utc)
        local_timestamp = timestamp.astimezone(UK_TZ)
        if start <= local_timestamp < end:
            result.append((timestamp, float(row[1])))
    return result


async def _diagnose_query_geometry(
    client: GlowmarktApiClient,
    resource_id: str,
    start: datetime,
    end: datetime,
) -> str:
    padded_rows = await client._request_readings(
        resource_id,
        start - timedelta(hours=1),
        end,
        "PT30M",
    )
    padded = _filter_local_day(padded_rows, start, end)
    padded_missing, padded_extra = _timestamp_geometry(start, end, padded)

    offset_minutes = -int(start.utcoffset().total_seconds() / 60)
    local_params = {
        "from": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "to": (end - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S"),
        "period": "PT30M",
        "offset": offset_minutes,
        "function": "sum",
        "nulls": 1,
    }
    local_payload = await client._get_json(
        f"{GLOWMARKT_API_BASE}/resource/{resource_id}/readings",
        params=local_params,
    )
    local_rows = local_payload.get("data", []) if isinstance(local_payload, dict) else []
    local = _filter_local_day(local_rows, start, end)
    local_missing, local_extra = _timestamp_geometry(start, end, local)

    return (
        f"padded_actual={len(padded)} padded_missing={padded_missing[:5]} "
        f"padded_extra={padded_extra[:5]}; local_offset={offset_minutes} "
        f"local_actual={len(local)} local_missing={local_missing[:5]} "
        f"local_extra={local_extra[:5]}"
    )


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
    """Compare protected live API data with the contributed immutable export."""
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
            reading = await client._fetch_day_reading(
                resource_id,
                start,
                end,
            )
            if reading is None:
                pytest.fail(f"Live contract: no completed-day data for case {day}")

            if len(reading.intervals) != expected["intervals"]:
                missing, extra = _timestamp_geometry(start, end, reading.intervals)
                alternatives = await _diagnose_query_geometry(
                    client,
                    resource_id,
                    start,
                    end,
                )
                pytest.fail(
                    "Live contract: interval geometry mismatch for case "
                    f"{day}; expected={expected['intervals']} actual={len(reading.intervals)} "
                    f"missing={missing[:5]} extra={extra[:5]}; {alternatives}"
                )
            if reading.value != expected["kwh"]:
                pytest.fail(f"Live contract: daily total mismatch for case {day}")
            if _canonical_digest(reading.intervals) != expected["sha256"]:
                pytest.fail(f"Live contract: interval fingerprint mismatch for case {day}")
