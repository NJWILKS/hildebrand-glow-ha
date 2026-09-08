from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.hildebrand_glow.api import DailyReading
from custom_components.hildebrand_glow.consumption_statistics import (
    build_consumption_statistics,
    consumption_metadata,
    energy_consumption_statistic_id,
)


def _reading(day: str, values: list[float]) -> DailyReading:
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    intervals = [
        (start + timedelta(minutes=30 * index), value)
        for index, value in enumerate(values)
    ]
    return DailyReading(day=day, value=sum(values), intervals=intervals)


def test_reset_rebuild_cannot_create_observed_negative_megawatt_hour_delta() -> None:
    """Regression for the -7.67 MWh Energy bar observed after a 2.3.3 reset.

    Historical cumulative use can be thousands of kWh. The first open-day row must
    continue that external sum; its state is only the hour's interval usage. It must
    never reinterpret the historical baseline itself as negative current-day use.
    """
    baseline = 7672.010
    current = _reading("2026-09-08", [0.21, 0.29])

    stats = build_consumption_statistics([current], baseline=baseline)

    assert len(stats) == 1
    assert stats[0]["state"] == 0.5
    assert stats[0]["sum"] == 7672.51
    assert stats[0]["sum"] >= baseline


def test_repeated_open_day_snapshot_is_idempotent() -> None:
    reading = _reading("2026-09-08", [0.2, 0.3, 0.4])

    first = build_consumption_statistics([reading], baseline=100.0)
    second = build_consumption_statistics([reading], baseline=100.0)

    assert first == second
    assert [(row["state"], row["sum"]) for row in first] == [
        (0.5, 100.5),
        (0.4, 100.9),
    ]


def test_same_hour_revision_replaces_shape_without_changing_ownership_model() -> None:
    first = build_consumption_statistics(
        [_reading("2026-09-08", [0.2])],
        baseline=50.0,
    )
    revised = build_consumption_statistics(
        [_reading("2026-09-08", [0.2, 0.35])],
        baseline=50.0,
    )

    assert first[0]["sum"] == 50.2
    assert revised[0]["state"] == 0.55
    assert revised[0]["sum"] == 50.55


def test_negative_consumption_interval_is_rejected() -> None:
    with pytest.raises(ValueError, match="Negative Glow consumption interval"):
        build_consumption_statistics(
            [_reading("2026-09-08", [0.2, -0.1])],
            baseline=100.0,
        )


def test_external_consumption_metadata_is_energy_compatible() -> None:
    metadata = consumption_metadata("site-123", "electricity")

    assert metadata["statistic_id"] == (
        "hildebrand_glow:site_123_electricity_energy_consumption"
    )
    assert metadata["source"] == "hildebrand_glow"
    assert metadata["has_sum"] is True
    assert metadata["unit_class"] == "energy"
    assert metadata["unit_of_measurement"] == "kWh"
    assert energy_consumption_statistic_id("Site 123", "electricity") == metadata[
        "statistic_id"
    ]
