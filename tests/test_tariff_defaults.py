from __future__ import annotations

from custom_components.hildebrand_glow.const import (
    CONF_ELECTRICITY_RATE,
    CONF_ELECTRICITY_STANDING_CHARGE,
    CONF_GAS_RATE,
    CONF_GAS_STANDING_CHARGE,
)
from custom_components.hildebrand_glow.tariff_defaults import latest_tariff_defaults


def test_latest_tariff_defaults_use_latest_effective_period_in_pounds() -> None:
    ledger = {
        "commodities": {
            "electricity": [
                {
                    "effectiveDate": "2026-04-01 00:00:00",
                    "plan": [{"planDetail": [{"rate": 23.1}, {"standing": 55.2}]}],
                },
                {
                    "effectiveDate": "2026-07-01 00:00:00",
                    "plan": [{"planDetail": [{"rate": 24.5}, {"standing": 58.2}]}],
                },
            ],
            "gas": [
                {
                    "effectiveDate": "2026-07-01 00:00:00",
                    "plan": [{"planDetail": [{"rate": 6.5}, {"standing": 31.0}]}],
                }
            ],
        }
    }

    defaults = latest_tariff_defaults(ledger)

    assert defaults == {
        CONF_ELECTRICITY_RATE: 0.245,
        CONF_ELECTRICITY_STANDING_CHARGE: 0.582,
        CONF_GAS_RATE: 0.065,
        CONF_GAS_STANDING_CHARGE: 0.31,
    }


def test_latest_tariff_defaults_do_not_flatten_tou_rate() -> None:
    ledger = {
        "commodities": {
            "electricity": [
                {
                    "effectiveDate": "2026-07-01 00:00:00",
                    "plan": [
                        {
                            "planDetail": [
                                {"standing": 58.2},
                                {"tier": 1, "rate": 30.0, "time": "05:00-23:59"},
                                {"tier": 2, "tourate": 8.0, "time": "00:00-05:00"},
                            ]
                        }
                    ],
                }
            ]
        }
    }

    defaults = latest_tariff_defaults(ledger)

    assert CONF_ELECTRICITY_RATE not in defaults
    assert defaults[CONF_ELECTRICITY_STANDING_CHARGE] == 0.582
