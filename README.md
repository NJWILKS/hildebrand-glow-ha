# Hildebrand Glow (Bright App) for Home Assistant

[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A maintained rescue fork of the Home Assistant integration for UK SMETS2 smart meters using the Hildebrand Glow / Bright API.

## What this fork adds

- Full historical electricity and gas consumption from Glow PT30M readings.
- Integration-owned external Home Assistant Energy statistics, avoiding Recorder/import ownership conflicts.
- A useful visible **Electricity Consumption Today** / **Gas Consumption Today** value instead of exposing a lifetime cumulative register.
- Rolling current-day PT30M ingestion with protection against shorter transient API windows moving Energy statistics backwards.
- DST-correct 46/48/50 interval handling.
- Effective-dated Glow tariff history.
- Separate integration-owned **Usage Cost** and **Standing Charge** external statistics suitable for stacked daily cost charts.
- Flat-tariff pricing from actual PT30M consumption × the effective unit rate, with Glow P1D cost retained as a reconciliation oracle.
- TOU/dynamic fallback to Glow PT30M cost where a single unit rate cannot correctly price the day.
- One-time cleanup of obsolete Recorder statistics left by earlier entity-backed sensor models.
- Multi-site Bright account support, API pacing/retry protection, safe reset/rebuild, and regression CI.

## Installation

### HACS custom repository

1. Open **HACS** in Home Assistant.
2. Open the three-dot menu and choose **Custom repositories**.
3. Add `https://github.com/NJWILKS/hildebrand-glow-ha` as an **Integration**.
4. Search for **Hildebrand Glow (Bright App)** and download it.
5. Restart Home Assistant.
6. Go to **Settings → Devices & Services → Add Integration** and search for **Hildebrand Glow**.

For a clean acceptance test, install this fork only. Do not keep another integration installed under the same `hildebrand_glow` domain.

## Configuration

The setup flow asks for:

- Bright email address and password;
- Bright meter site / location;
- electricity unit rate and standing charge;
- gas unit rate and standing charge.

Configured tariff values are calibration/fallback values. The integration also retrieves Glow's effective-dated tariff history and prefers explicit tariff information where available.

The options menu exposes polling/tariff settings and **Reset imported history**. Reset clears only this integration's imported statistics and local ingestion state, preserves credentials/entities/dashboards, reloads the entry and rebuilds from Glow.

## Consumption and Energy history

Glow PT30M readings are interval energy. They are **not** treated as a physical lifetime meter register.

Version 2.3.4 uses one owner for Home Assistant Energy history:

- the integration writes a dedicated external statistic such as `hildebrand_glow:<site>_electricity_energy_consumption`;
- each dated interval/hour has a meaningful contextual `state` in kWh;
- Home Assistant's required cumulative `sum` is maintained internally by the integration;
- the visible consumption sensor shows **today's published consumption**, not the lifetime cumulative sum;
- the visible sensor has no Recorder state class, so Recorder cannot create a competing long-term statistic for it.

From 2.3.6, an external Hildebrand consumption source is also paired automatically with its matching external total-cost statistic. Stale fixed/entity price fields left behind by older Home Assistant Energy preferences are cleared when no explicit alternative cost statistic is configured.

### Fresh install / reset

The integration:

1. uses Glow `first-time` only as an approximate locator;
2. asks the PT30M readings endpoint for the first actual available interval;
3. retrieves available history in bounded DST-safe chunks;
4. rebuilds the integration-owned external Energy statistic;
5. migrates Home Assistant Energy configuration away from the legacy sensor statistic ID where applicable;
6. continues writing the same external statistic for the current UK-local day.

A genuine `0.0` interval counts as data. Missing/null readings do not.

### Rolling current day

Today's consumption is recalculated from the published PT30M intervals on every poll rather than incrementally adding the latest reading. Repeating the same response is therefore idempotent.

If Bright temporarily returns fewer current-day intervals than were previously published, the integration preserves the last good Energy rows instead of moving the external cumulative sum backwards.

Closed days are reconciled in order and require the expected UK-local PT30M geometry:

- normal day: **48** intervals;
- spring DST day: **46** intervals;
- autumn DST day: **50** intervals.

## Cost and standing-charge model

The end result is a daily stack of:

- **Usage Cost**;
- **Standing Charge**.

For a flat tariff:

`usage cost = Σ(PT30M kWh × effective unit rate)`

`daily total = usage cost + effective standing charge`

Glow `tariff-list` is treated as an effective-dated tariff ledger. A flat tariff may contain a `tier: 1` marker; that marker alone does not make the tariff time-of-use. TOU classification requires timed/TOU rate structure, while dynamic tariffs remain distinct.

For TOU or dynamic tariffs, Glow PT30M cost remains the authoritative usage-cost shape because one flat rate cannot describe the day correctly.

The standing charge is applied exactly once per UK-local billing day when the effective tariff is known, including the current open day. Glow P1D cost is retained as a completed-day reconciliation oracle rather than being used to distort tariff-derived components.

The total Energy cost and both chart components are integration-owned external statistics:

- `hildebrand_glow:<site>_electricity_energy_cost`;
- `hildebrand_glow:<site>_electricity_usage_cost`;
- `hildebrand_glow:<site>_electricity_standing_charge`.

Gas uses the equivalent `_gas_...` statistic IDs. The 2.3.6 cost schema rebuilds these external cost statistics under the new ownership model.

## Upgrade cleanup

All visible Hildebrand sensors are presentation/diagnostic entities in 2.3.6 and deliberately have **no Recorder state class**. Long-term consumption and cost history belongs only to the integration-owned external statistics.

On the first 2.3.6 setup for a meter site, the integration removes obsolete Recorder statistics attached to its visible sensor entity IDs. This cleanup:

- runs once per site;
- removes statistics metadata/data, not the sensor entities themselves;
- preserves entity registry entries, dashboards and credentials;
- does not delete current external consumption or cost statistics;
- does not use broad name/prefix matching, so unrelated Home Assistant statistics are not touched.

The separate cost-ingestion schema migration remains responsible for rebuilding the integration-owned cost statistics when their schema changes.

## Native stacked cost graph

Home Assistant's built-in Statistics Graph card accepts external statistic IDs, so the two component statistics can be displayed directly as stacked daily bars. Use the exact IDs shown under **Developer Tools → Statistics** for your site:

```yaml
type: statistics-graph
title: Electricity Cost
chart_type: bar-stack
period: day
stat_types:
  - change
entities:
  - entity: hildebrand_glow:<site>_electricity_usage_cost
    name: Usage
  - entity: hildebrand_glow:<site>_electricity_standing_charge
    name: Standing charge
```

`change` is used deliberately: each external statistic keeps a monotonic internal `sum`, so the daily change is exactly that day's usage-cost or standing-charge contribution.

## Sensors

The integration creates, where the corresponding resource exists:

- Electricity Consumption Today / Gas Consumption Today;
- electricity and gas API cost sensors;
- daily cost sensors;
- separate current-day usage-cost sensors;
- separate current-day standing-charge sensors;
- combined daily standing charge and total daily energy-cost sensors.

These visible entities are presentation/diagnostic surfaces only. None carries a Recorder state class. Historical Energy and cost charting should use the integration-owned external statistics rather than the visible sensor entity IDs.

If Glow exposes a resource but no usable reading is currently available, the entity remains **Unknown** rather than being forced to zero.

## Real-data regression contract

Normal pull-request CI uses no Bright credentials. It runs pytest, Ruff across the production integration and tests, Hassfest and HACS validation.

A separate protected live contract validates the final API semantics against a real Bright account without printing credentials, resource IDs, household readings or tariff values. It checks:

- first/last history boundaries;
- PT30M timestamp geometry, including DST transition days;
- completed-day cost aggregation semantics;
- effective-dated tariff availability;
- tariff classification;
- flat-rate consumption × unit-rate pricing;
- standing-charge reconciliation against Glow P1D.

See [docs/TESTING.md](docs/TESTING.md) and [docs/RELEASE_2_3_4_AUDIT.md](docs/RELEASE_2_3_4_AUDIT.md) for the regression/release gates.

## Architecture

The statistics ownership and source-of-truth rules are documented in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/STATISTICS_CONTRACT.md](docs/STATISTICS_CONTRACT.md).

## Development

Bug fixes should include regression coverage. Historical/cumulative changes require particular care around restart behaviour, duplicate imports, missing readings, UK-local day boundaries, DST, tariff transitions and API revisions.

Repository maintenance rules are documented in [AGENTS.md](AGENTS.md).

## Acknowledgements

This fork builds on the work of the original `xmcdanx` / `McDon22` Hildebrand Glow integration and its contributors. Operational patterns were also reviewed against `jonandel/ha-hildebrandglow-dcc`.

The project remains MIT licensed; see [LICENSE](LICENSE).
