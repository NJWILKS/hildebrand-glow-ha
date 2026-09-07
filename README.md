# Hildebrand Glow (Bright App) for Home Assistant

[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A maintained rescue fork of the Home Assistant integration for UK SMETS2 smart meters using the Hildebrand Glow / Bright API.

## What this fork adds

- **Full historical electricity and gas consumption import** into Home Assistant Recorder statistics using the original half-hour timestamps.
- **Historical cost-component import**: Glow PT30M cost is treated as usage-only and completed P1D cost as usage plus standing charge, allowing the standing charge to be observed rather than guessed.
- **Native Home Assistant stacked cost graphs** using separate Usage Cost and Standing Charge monetary sensors with backfilled Recorder statistics.
- **Effective-dated tariff history** from Glow `tariff-list`, persisted locally and refreshed daily so rate/cap changes are not flattened into today's tariff.
- **Full-history discovery without an arbitrary look-back limit**: Glowmarkt `first-time` is used as a cheap locator, then the PT30M readings endpoint determines the first actual billing interval.
- **DST-safe PT30M history retrieval** in bounded chunks suitable for the Glowmarkt API.
- **Multi-site Bright account support** with explicit meter-site selection.
- **Stable entity identity** that prefers the real Glow resource ID, so re-adding a site does not create a new logical meter unnecessarily.
- **API resilience**: one paced request lane, `Retry-After` support, bounded retries for HTTP 429, transient 5xx responses and transient connection drops. API failures are never interpreted as an empty history boundary.
- **Lower API pressure**: consumption defaults to 15-minute polling; API-derived cost resources default to 60 minutes; both are configurable with a 5-minute minimum.
- **Non-blocking history backfill** so Home Assistant setup is not held open while historical data is retrieved.
- **Home Assistant validation and regression CI** with pytest, Ruff, Hassfest and HACS validation.

## Installation

### HACS custom repository

1. Open **HACS** in Home Assistant.
2. Open the three-dot menu and choose **Custom repositories**.
3. Add `https://github.com/NJWILKS/hildebrand-glow-ha` as an **Integration**.
4. Search for **Hildebrand Glow (Bright App)** and download it.
5. Restart Home Assistant.
6. Go to **Settings → Devices & Services → Add Integration** and search for **Hildebrand Glow**.

For a clean acceptance test, install this fork only; do not keep a second copy of another integration under the same `hildebrand_glow` domain.

## Configuration

The setup flow asks for:

- Bright email address and password
- Bright meter site / location
- electricity unit rate and standing charge
- gas unit rate and standing charge

The configured tariff values are **fallback values**. When Glow cost resources are available, the API-derived cost is authoritative and Glow's effective-dated `tariff-list` is stored separately as the historical tariff ledger.

After setup, **Configure** also exposes:

- consumption refresh interval, default **15 minutes**
- API-cost refresh interval, default **60 minutes**

Intervals below five minutes are blocked to reduce the risk of Glowmarkt HTTP 429 responses.

## Energy history model

The consumption sensors are cumulative `TOTAL_INCREASING` energy sensors for Home Assistant. Historical Bright data is imported directly into Recorder statistics using the original half-hour readings, aggregated into Home Assistant's hourly statistics while preserving the real shape of the day.

On a fresh install the integration:

1. asks Glowmarkt `first-time` for an approximate start locator;
2. queries a small PT30M window around that locator and treats the earliest non-null reading as the real start of available billing history;
3. retrieves all complete history from that resolved boundary to today in DST-safe PT30M chunks;
4. imports the hourly statistics in the background;
5. stores cumulative day state so restarts do not add the same completed day twice.

A genuine `0.0` reading counts as data. Missing/null readings do not. The current incomplete UK-local day is not treated as completed historical data.

`first-time` is deliberately **not** treated as authoritative consumption data. Live testing showed that Glowmarkt metadata can continue to point at historical intervals which the readings endpoint no longer returns. The PT30M readings endpoint therefore decides which intervals actually exist for billing/history purposes.

If Glowmarkt temporarily fails, disconnects or rate-limits a request, the integration retries with bounded backoff. A failed request is never converted into a false end-of-history marker.

For implementation detail, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Cost and standing-charge model

Bright/Glow cost resources have different aggregation semantics:

- **PT30M and hourly cost** represent usage cost only; standing charge is not included.
- **P1D, weekly and monthly cost** include standing charge.

The integration therefore does not assume that a configured standing charge has already been applied. For a completed UK-local day it calculates:

`observed standing charge = Glow P1D cost - sum(Glow PT30M cost)`

The Glow P1D total remains authoritative. The residual is used only to separate the bill into usage and standing-charge components.

For the current partial day, the integration uses PT30M cost only and reports a standing-charge component of **£0** until Glow publishes a completed P1D bucket. If one side of the reconciliation is missing or contradictory, the standing-charge split is reported as unavailable/unknown instead of being invented.

Historical cost backfill fetches PT30M cost in DST-safe bounded chunks and P1D cost in bounded daily chunks, joins them by UK-local date and imports separate historical states for:

- **Electricity Usage Cost**
- **Electricity Standing Charge**
- **Gas Usage Cost**
- **Gas Standing Charge**

These are monetary `TOTAL` sensors with long-term Recorder statistics.

### Native stacked cost graph

Home Assistant's built-in Statistics Graph card supports stacked bars, so no custom dashboard card is required. After the entities have been created, a dashboard card can use:

```yaml
type: statistics-graph
title: Electricity Cost
chart_type: bar-stack
period: day
stat_types:
  - state
entities:
  - sensor.smart_meter_electricity_usage_cost
  - sensor.smart_meter_electricity_standing_charge
```

If Home Assistant chose different entity IDs, select the two corresponding entities in the visual card editor or adjust the YAML.

The same card can use `week`, `month` or `year` periods, and can be tied to an Energy Date Selection card using `energy_date_selection: true`.

## Tariff history

Glow's `tariff-list` is treated as an effective-dated ledger rather than a set of constants. The integration stores the returned tariff history per commodity and refreshes it daily. That preserves historical rate/standing-charge changes such as price-cap changes without recalculating old bills using today's tariff.

The **cost API remains authoritative for actual historical charges**. The tariff ledger explains which tariff was effective; it does not overwrite API-derived historical costs.

## Sensors

The integration creates electricity and gas consumption sensors, API cost sensors when those resources exist, daily cost sensors, separate usage/standing-charge component sensors and combined standing-charge totals.

Glow/Bright DCC data is not real-time and can arrive a day or more late. The coordinator therefore looks for the latest completed day containing actual readings rather than assuming yesterday is already complete.

## Real-data regression contract

The test suite includes a privacy-safe oracle derived from a contributed real Bright electricity export containing **19,346 half-hour readings** across 404 UK-local days. The raw household CSV is not stored in the repository.

The protected live test uses repository environment credentials and verifies:

- Glowmarkt `first-time` still locates the known historical neighbourhood;
- the PT30M readings endpoint resolves an actual first available interval near that locator;
- `last-time` has not regressed behind the known export;
- known completed days retain the exact expected half-hour timestamp geometry, including **50** intervals on the autumn DST day and **46** on the spring DST day;
- a known completed electricity-cost day has a positive P1D-minus-PT30M standing-charge residual;
- `tariff-list` returns effective-dated tariff history for the known electricity cost resource.

The contributed CSV is a historical snapshot, not an immutable billing ledger. Live testing proved that Glowmarkt can revise or remove old consumption values while preserving the same timestamp geometry and `first-time` metadata. The **current PT30M readings API is therefore authoritative for consumption values**; old CSV totals and value fingerprints are useful evidence, but they are not release gates.

The live job checks boundaries, timestamp geometry and cost semantics without printing raw household readings, tariff values or credentials to Actions logs. It is **manual-only** and runs separately from normal pull-request CI.

See [docs/TESTING.md](docs/TESTING.md) for the test strategy and live-contract rules.

## Development

Normal pull-request CI never uses Bright credentials. It runs mocked/unit tests, Ruff, Hassfest and HACS validation. The protected live contract is separate and uses the `glow-live` GitHub Environment.

Bug fixes should arrive with a regression test. Historical/cumulative energy and cost changes require particular care around duplicate imports, restart behaviour, missing readings, UK-local day boundaries, DST and API revisions.

Repository/agent maintenance rules are documented in [AGENTS.md](AGENTS.md).

## Acknowledgements

This fork builds on the work of the original `xmcdanx` / `McDon22` Hildebrand Glow integration and its contributors. Operational patterns were also reviewed against `jonandel/ha-hildebrandglow-dcc`, including resource-based identity, separated polling concerns and API-protective polling intervals.

The project remains MIT licensed; see [LICENSE](LICENSE).
