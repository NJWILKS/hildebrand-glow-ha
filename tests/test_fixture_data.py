from __future__ import annotations

import json
from pathlib import Path

ORACLE = Path(__file__).parent / "fixtures" / "electricity_export_oracle.json"


def _load_oracle() -> dict:
    return json.loads(ORACLE.read_text(encoding="utf-8"))


def test_export_oracle_captures_full_source_dataset() -> None:
    oracle = _load_oracle()

    assert oracle["source_rows"] == 19346
    assert oracle["source_total_kwh"] == 7646.171
    assert oracle["zero_readings"] == 1
    assert oracle["uk_local_days"] == 404
    assert oracle["uk_local_start"] == "2025-07-30T00:00:00+01:00"
    assert oracle["uk_local_end"] == "2026-09-06T00:30:00+01:00"


def test_export_oracle_covers_both_uk_dst_transition_days() -> None:
    known_days = _load_oracle()["known_days"]

    assert known_days["2025-10-26"] == {"intervals": 50, "kwh": 20.989}
    assert known_days["2026-03-29"] == {"intervals": 46, "kwh": 24.096}


def test_export_oracle_has_known_boundary_day_totals() -> None:
    known_days = _load_oracle()["known_days"]

    assert known_days["2025-07-30"] == {"intervals": 48, "kwh": 22.026}
    assert known_days["2026-09-05"] == {"intervals": 48, "kwh": 11.373}
    assert known_days["2026-09-06"] == {"intervals": 2, "kwh": 0.307}
