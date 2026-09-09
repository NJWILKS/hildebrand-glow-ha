from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.hildebrand_glow.const import (
    CONF_ELECTRICITY_RATE,
    CONF_ELECTRICITY_STANDING_CHARGE,
    CONF_GAS_RATE,
)
from custom_components.hildebrand_glow.tariff_defaults import (
    async_latest_tariff_defaults,
    latest_tariff_defaults,
)


def test_latest_tariff_defaults_rejects_malformed_ledger_shapes() -> None:
    assert latest_tariff_defaults({"commodities": []}, as_of=date(2026, 9, 1)) == {}
    assert latest_tariff_defaults(
        {"commodities": {"electricity": "bad", "gas": None}},
        as_of=date(2026, 9, 1),
    ) == {}


def test_latest_tariff_defaults_ignores_future_periods_and_non_dict_rows() -> None:
    ledger = {
        "commodities": {
            "electricity": [
                "bad-row",
                {
                    "effectiveDate": "2026-10-01",
                    "plan": [{"rate": 30.0, "standing": 60.0}],
                },
            ],
            "gas": [
                {
                    "effectiveDate": "2026-08-01",
                    "plan": [{"rate": 6.5}],
                }
            ],
        }
    }

    result = latest_tariff_defaults(ledger, as_of=date(2026, 9, 1))

    assert CONF_ELECTRICITY_RATE not in result
    assert CONF_ELECTRICITY_STANDING_CHARGE not in result
    assert result[CONF_GAS_RATE] == 0.065


def test_latest_tariff_defaults_can_return_standing_without_flat_rate() -> None:
    ledger = {
        "commodities": {
            "electricity": [
                {
                    "effectiveDate": "2026-07-01",
                    "plan": [
                        {
                            "standing": 58.2,
                            "rate": 30.0,
                            "tourate": 8.0,
                            "time": "00:00-05:00",
                        }
                    ],
                }
            ]
        }
    }

    result = latest_tariff_defaults(ledger, as_of=date(2026, 9, 1))

    assert CONF_ELECTRICITY_RATE not in result
    assert result[CONF_ELECTRICITY_STANDING_CHARGE] == 0.582


@pytest.mark.asyncio
async def test_async_latest_tariff_defaults_handles_empty_store_and_passes_loaded_ledger(hass) -> None:
    store = AsyncMock()
    store.async_load.side_effect = [None, {"commodities": {}}]

    with patch(
        "custom_components.hildebrand_glow.tariff_defaults.Store",
        return_value=store,
    ) as store_type:
        assert await async_latest_tariff_defaults(hass, "site-1") == {}
        assert await async_latest_tariff_defaults(hass, "site-1") == {}

    assert store.async_load.await_count == 2
    assert store_type.call_count == 2
    args = store_type.call_args.args
    assert args[2].endswith("site-1_tariff_history")
