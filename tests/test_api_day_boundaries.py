from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.hildebrand_glow.api import UK_TZ, GlowmarktApiClient


@pytest.mark.asyncio
async def test_completed_day_excludes_api_bucket_at_next_midnight(monkeypatch) -> None:
    client = GlowmarktApiClient("user", "password", object())  # type: ignore[arg-type]
    client._token = "test-token"
    client._token_expiry = datetime.now() + timedelta(days=1)

    day_start = datetime(2026, 9, 5, tzinfo=UK_TZ)
    day_end = day_start + timedelta(days=1)
    first_epoch = int(day_start.astimezone(timezone.utc).timestamp())
    boundary_epoch = int(day_end.astimezone(timezone.utc).timestamp())

    async def fake_readings(*args, **kwargs):
        return [
            [first_epoch, 0.4],
            [first_epoch + 1800, 0.6],
            [boundary_epoch, 99.0],
        ]

    monkeypatch.setattr(client, "_request_readings", fake_readings)

    reading = await client._fetch_day_reading(
        "electricity-resource",
        day_start,
        day_end,
    )

    assert reading is not None
    assert reading.day == "2026-09-05"
    assert reading.value == 1.0
    assert len(reading.intervals) == 2
    assert all(timestamp < day_end for timestamp, _ in reading.intervals)
