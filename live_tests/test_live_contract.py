from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import pytest

from custom_components.hildebrand_glow.api import UK_TZ, GlowmarktApiClient
from custom_components.hildebrand_glow.const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_ELECTRICITY_COST,
    GLOWMARKT_API_BASE,
)
from custom_components.hildebrand_glow.costing import _window_total, get_cost_history
from custom_components.hildebrand_glow.tariff import derive_tariff_periods
from custom_components.hildebrand_glow.tariff_costing import price_cost_history

pytestmark = pytest.mark.live

ORACLE = (
    Path(__file__).parents[1]
    / "tests"
    / "fixtures"
    / "electricity_export_oracle.json"
)
LOCATOR_TOLERANCE = timedelta(days=3)
GEOMETRY_TEST_DAYS = (
    "2025-10-26",
    "2026-03-29",
    "2026-09-05",
)
COST_SEMANTICS_DAY = "2026-09-05"
TARIFF_USAGE_TOLERANCE_PENCE = 5.0
TARIFF_TOTAL_TOLERANCE_PENCE = 15.0


def _oracle() -> dict:
    return json.loads(ORACLE.read_text(encoding="utf-8"))


def _expected_epochs(start: datetime, end: datetime) -> list[int]:
    """Return the exact UTC half-hour geometry for one UK-local day."""
    start_epoch = int(start.astimezone(timezone.utc).timestamp())
    end_epoch = int(end.astimezone(timezone.utc).timestamp())
    return list(range(start_epoch, end_epoch, 1800))


async def _known_electricity_resources(
    client: GlowmarktApiClient,
    expected_first_epoch: int,
) -> tuple[str, str, datetime]:
    """Find known consumption and cost resources using first-time as a locator."""
    virtual_entities = await client.get_virtual_entities()
    if not virtual_entities:
        pytest.fail("Live contract: no Bright meter sites were returned")

    expected = datetime.fromtimestamp(expected_first_epoch, tz=timezone.utc)
    for virtual_entity in virtual_entities:
        virtual_entity_id = virtual_entity.get("veId")
        if not virtual_entity_id:
            continue
        resources = await client.discover_resources(virtual_entity_id)
        electricity = resources.get(CLASSIFIER_ELECTRICITY_CONSUMPTION)
        electricity_cost = resources.get(CLASSIFIER_ELECTRICITY_COST)
        if not electricity or not electricity_cost:
            continue
        resource_id = electricity["resource_id"]
        locator = await client.get_first_reading_time(resource_id)
        if locator is not None and abs(locator - expected) <= LOCATOR_TOLERANCE:
            return resource_id, electricity_cost["resource_id"], locator

    pytest.fail("Live contract: known electricity consumption/cost resources not found")


@pytest.mark.asyncio
async def test_live_account_matches_known_electricity_export() -> None:
    """Validate current billing API geometry, tariffs and cost semantics."""
    oracle = _oracle()
    username = os.environ["GLOWMARKT_USERNAME"]
    password = os.environ["GLOWMARKT_PASSWORD"]

    async with aiohttp.ClientSession() as session:
        client = GlowmarktApiClient(username, password, session)
        if not await client.authenticate():
            pytest.fail("Live contract: authentication failed")

        resource_id, cost_resource_id, locator = await _known_electricity_resources(
            client,
            oracle["first_epoch_utc"],
        )

        # first-time is a locator only. The readings endpoint is authoritative for
        # the first actual billable interval because historical rows can be revised
        # or disappear independently of first-time metadata.
        first_actual = await client.get_first_available_reading_time(resource_id)
        if first_actual is None:
            pytest.fail("Live contract: no actual reading found near first-time locator")
        if first_actual < locator - LOCATOR_TOLERANCE:
            pytest.fail("Live contract: actual history starts unexpectedly before locator")
        if first_actual > locator + LOCATOR_TOLERANCE:
            pytest.fail("Live contract: actual history starts too far after first-time locator")

        last = await client.get_last_reading_time(resource_id)
        if last is None or int(last.timestamp()) < oracle["last_epoch_utc"]:
            pytest.fail("Live contract: API last-time is earlier than the known export")

        # The contributed CSV is a historical snapshot, not an immutable billing
        # ledger. Glow can revise historic values while retaining the same timestamp
        # geometry. Current PT30M API values are therefore authoritative; the oracle
        # proves we are querying the same known days and preserving every expected
        # half-hour, especially across both UK DST transitions.
        for day in GEOMETRY_TEST_DAYS:
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

            actual_epochs = [int(timestamp.timestamp()) for timestamp, _ in reading.intervals]
            expected_epochs = _expected_epochs(start, end)

            if len(actual_epochs) != expected["intervals"]:
                pytest.fail(f"Live contract: interval count mismatch for case {day}")
            if actual_epochs != expected_epochs:
                pytest.fail(f"Live contract: timestamp geometry mismatch for case {day}")

        # Bright cost aggregation semantics: PT30M is usage-only while P1D contains
        # the completed-day standing charge.
        cost_start = datetime.fromisoformat(COST_SEMANTICS_DAY).replace(tzinfo=UK_TZ)
        cost_end = cost_start + timedelta(days=1)
        pt30m_rows = await client._request_readings(
            cost_resource_id,
            cost_start,
            cost_end,
            "PT30M",
        )
        p1d_rows = await client._request_readings(
            cost_resource_id,
            cost_start,
            cost_end,
            "P1D",
        )
        usage_cost = _window_total(pt30m_rows, cost_start, cost_end)
        daily_cost = _window_total(p1d_rows, cost_start, cost_end)
        if usage_cost is None or daily_cost is None:
            pytest.fail("Live contract: cost aggregation data missing for completed day")
        if daily_cost - usage_cost <= 1.0:
            pytest.fail("Live contract: P1D cost did not contain a standing-charge residual")

        tariff = await client._get_json(
            f"{GLOWMARKT_API_BASE}/resource/{cost_resource_id}/tariff-list"
        )
        if not isinstance(tariff, dict) or tariff.get("status") not in (None, "OK"):
            pytest.fail("Live contract: tariff-list endpoint returned an error")
        tariff_rows = tariff.get("data")
        if not isinstance(tariff_rows, list) or not tariff_rows:
            pytest.fail("Live contract: tariff-list returned no effective-dated tariff history")

        recent_start = datetime.now(UK_TZ) - timedelta(days=35)
        recent_history = await get_cost_history(
            client,
            cost_resource_id,
            start_uk=recent_start,
        )
        periods = derive_tariff_periods(
            [row for row in tariff_rows if isinstance(row, dict)],
            recent_history,
        )
        candidates = [
            period
            for period in periods
            if period.standing_source == "tariff_list"
            and period.standing_pence is not None
            and period.residual_median_pence is not None
            and period.sample_days >= 5
        ]
        if not candidates:
            pytest.fail(
                "Live contract: no recent tariff period had enough standing-charge evidence"
            )
        calibrated = max(candidates, key=lambda period: period.sample_days)
        tolerance = max(15.0, 6.0 * float(calibrated.residual_mad_pence or 0.0))
        if (
            abs(
                float(calibrated.standing_pence)
                - float(calibrated.residual_median_pence)
            )
            > tolerance
        ):
            pytest.fail(
                "Live contract: tariff standing charge did not match residual cluster"
            )

        # 2.3.4's end-game contract: for a flat tariff, consumption × the exact
        # effective unit rate plus the exact standing charge must reproduce Bright's
        # cost resources closely enough that the stacked Usage + Standing chart is
        # billing-real rather than an inferred visual approximation.
        contract_reading = await client._fetch_day_reading(
            resource_id,
            cost_start,
            cost_end,
        )
        contract_breakdown = next(
            (
                item
                for item in recent_history
                if item.day == COST_SEMANTICS_DAY
            ),
            None,
        )
        if contract_reading is None or contract_breakdown is None:
            pytest.fail("Live contract: tariff pricing day is not available")

        priced, diagnostics = price_cost_history(
            [contract_breakdown],
            [contract_reading],
            periods,
        )
        if not priced or not diagnostics:
            pytest.fail("Live contract: tariff pricing produced no result")
        diagnostic = diagnostics[0]
        if diagnostic["rate_kind"] != "flat":
            pytest.fail("Live contract: known account no longer exposes a flat tariff")
        if diagnostic["unit_rate_pence_per_kwh"] is None:
            pytest.fail("Live contract: flat tariff unit rate was not resolved")
        if diagnostic["standing_pence"] is None:
            pytest.fail("Live contract: standing charge was not resolved")

        priced_usage = priced[0].usage_pence
        if priced_usage is None:
            pytest.fail("Live contract: tariff-priced usage was unavailable")
        if abs(float(priced_usage) - float(usage_cost)) > TARIFF_USAGE_TOLERANCE_PENCE:
            pytest.fail(
                "Live contract: consumption x tariff unit rate does not match PT30M cost"
            )
        if (
            abs(float(priced[0].total_pence) - float(daily_cost))
            > TARIFF_TOTAL_TOLERANCE_PENCE
        ):
            pytest.fail(
                "Live contract: tariff-priced usage + standing does not match P1D cost"
            )
