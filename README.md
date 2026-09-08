# Hildebrand Glow (Bright App) for Home Assistant

[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A maintained rescue fork of the Home Assistant integration for UK SMETS2 smart meters using the Hildebrand Glow / Bright API.

## What this fork adds

- **Full historical electricity and gas consumption import** into Home Assistant Recorder statistics using the original half-hour timestamps.
- **Rolling current-day PT30M consumption**: as Bright publishes new half-hour readings, the live cumulative sensor advances without waiting for the day to finish.
- **Completed-day reconciliation**: missing closed days are repaired before current-day data is applied, with DST-correct 46/48/50 interval validation.
- **Rolling current-day cost** from Bright's PT30M usage-cost snapshots, followed by authoritative P1D reconciliation after the day closes.
- **Historical cost-component import**: Glow P1D remains the authoritative daily bill, while effective-dated tariff standing charges are used to split that bill into usage and standing-charge components without preserving noisy day-by-day aggregation residuals.
- **Native Home Assistant stacked cost graphs** using separate Usage Cost and Standing Charge monetary sensors with backfilled Recorder statistics.
- **Effective-dated tariff history** from Glow `tariff-list`, persisted locally and refreshed daily so rate/cap changes are not flattened into today's tariff.
- **Full-history discovery without an arbitrary look-back limit**: Glowmarkt `first-time` is used as a cheap locator, then the PT30M readings endpoint determines the first actual billing interval.
- **DST-safe PT30M history retrieval** in bounded chunks suitable for the Glowmarkt API.
- **Multi-site Bright account support** with explicit meter-site selection.
- **Stable entity identity** that prefers the real Glow resource ID, so re-adding a site does not create a new logical meter unnecessarily.
- **API resilience**: one paced request lane, `Retry-After` support, bounded retries for HTTP 429, transient 5xx responses and transient connection drops. API failures are never interpreted as an empty history boundary.
- **Protection against fake meter resets**: a transient shorter/no current-day Bright response cannot move the live `TOTAL_INCREASING` sensor backwards.
- **Lower API pressure**: consumption defaults to 15-minute polling; API-derived cost resources default to 60 minutes; both are configurable with a 5-minute minimum.
- **Non-blocking history backfill** so Home Assistant setup is not held open while historical data is retrieved.
- **Safe imported-history reset** that clears only this integration's statistics/backfill state while preserving credentials, entities, dashboards and unrelated history.
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

The configured tariff values are **current-tariff calibration and fallback values**. When Glow cost resources are available, Glow P1D cost is authoritative for the amount actually charged. The configured current values are compared with the latest effective tariff period and can supply an exact current value when Glow's historical plan detail is incomplete but the residual evidence agrees.

The integration options menu exposes:

- **Tariff and polling settings**
  - consumption refresh interval, default **15 minutes**
  - API-cost refresh interval, default **60 minutes**
  - tariff calibration/fallback values
- **Reset imported history**
  - clears this config entry's imported Recorder statistics and local backfill/tariff state
  - preserves credentials, entity registry entries, dashboards and unrelated Home Assistant history
  - reloads the entry so the normal ingestion pipeline rebuilds the data from Glow

Intervals below five minutes are blocked to reduce the risk of Glowmarkt HTTP 429 responses.

## Energy history model

The consumption sensors are cumulative `TOTAL_INCREASING` energy sensors for Home Assistant.

There are deliberately two ownership zones:

- **completed historical days** are imported by the integration into Recorder statistics;
- **the current UK-local day** is recorded naturally from the live cumulative sensor as Bright publishes PT30M readings.

The integration does not manually import current-day consumption statistics. That avoids having both the history importer and Home Assistant Recorder write different meanings into the same statistic.

### Fresh install / reset

On a fresh install or imported-history reset the integration:

1. asks Glowmarkt `first-time` for an approximate start locator;
2. queries a small PT30M window around that locator and treats the earliest non-null reading as the real start of available billing history;
3. retrieves the complete closed-day history from that resolved boundary in DST-safe PT30M chunks;
4. imports the completed hourly statistics in the background;
5. stores an explicit completed-day cumulative baseline;
6. performs a normal refresh, which adds today's currently published PT30M total to that baseline for the live sensor.

A genuine `0.0` reading counts as data. Missing/null readings do not.

`first-time` is deliberately **not** treated as authoritative consumption data. Live testing showed that Glowmarkt metadata can continue to point at historical intervals which the readings endpoint no longer returns. The PT30M readings endpoint therefore decides which intervals actually exist for billing/history purposes.

### Rolling current day

Every consumption poll recalculates the open day from source:

`completed historical cumulative + sum(today's published PT30M intervals)`

It does not incrementally add "the latest value". Repeating the same Bright response is therefore idempotent and a restart reconstructs the same state without double counting.

Only completed half-hour intervals are included. If Bright temporarily returns fewer current-day intervals than the integration already saw, the last good cumulative value is preserved instead of allowing the `TOTAL_INCREASING` sensor to fall and look like a meter reset. Same-count revisions are accepted so genuine Bright corrections can still propagate.

### Completed-day reconciliation

Before today's rolling value is applied, the integration repairs every closed but unreconciled day in sequence.

A day is considered complete only when its UK-local PT30M geometry is complete:

- normal day: **48** intervals
- spring DST day: **46** intervals
- autumn DST day: **50** intervals

If a closed day is still incomplete, reconciliation stops there and the previous good live value is retained. Later dates are not skipped because the cumulative baseline would otherwise become ambiguous.

If Glowmarkt temporarily fails, disconnects or rate-limits a request, the integration retries with bounded backoff. A failed request is never converted into zero consumption or a false end-of-history marker.

For implementation detail, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Cost and standing-charge model

Bright/Glow cost resources have different aggregation semantics:

- **PT30M and hourly cost** represent usage cost only; standing charge is not included.
- **P1D, weekly and monthly cost** include standing charge.

Version 2.3 uses that publication model directly.

### Current open day

As Bright publishes PT30M cost snapshots, the integration exposes the current day's usage cost and updates the dedicated Energy-dashboard cost statistic provisionally.

The standing-charge component is **£0 for the open day** because Bright's PT30M values do not contain standing charge. The integration does not invent or pre-apply it.

### Completed day

After the day closes, the cost pipeline first waits for Glow P1D. Once available:

1. **Glow P1D is authoritative for the completed day's total bill.**
2. PT30M data preserves the intraday cost shape.
3. Glow `tariff-list` effective dates and plan details define historical tariff periods.
4. When a tariff period exposes `standing` directly, that value is used throughout that period.
5. If a historical period does not expose a standing charge, the integration can infer one stable value from the median positive `P1D - sum(PT30M)` residuals within that exact period.
6. The configured current standing charge and unit rate are calibration/fallback anchors for the latest period.
7. **Daily usage cost becomes `P1D total - resolved standing charge`**, so usage plus standing charge exactly reconciles to the authoritative P1D bill.

The separately aggregated P1D and PT30M values can contain small day-to-day rounding/aggregation differences. Treating each raw residual as a new standing charge would produce an unrealistic saw-tooth graph, so standing charges are normalised by effective tariff period instead.

The result is a piecewise-constant standing-charge history: stable within a tariff period and changing only at a real effective tariff boundary. Flat unit rates are recorded where Glow exposes one; time-of-use and dynamic tariffs retain their tariff type instead of being flattened to a single rate.

### Historical cost statistics

The integration imports historical states for:

- **Electricity Usage Cost**
- **Electricity Standing Charge**
- **Gas Usage Cost**
- **Gas Standing Charge**

These monetary `TOTAL` sensors use long-term Recorder statistics with the same daily-reset/hourly semantics as their live states. The dedicated Energy-dashboard cumulative cost statistic is integration-owned and remains based on the exact Glow P1D total for completed days.

Version **2.3.0** advances both the consumption and cost ingestion schemas. Existing legacy statistics are rebuilt once so 2.1/2.2 synthetic current-day rows and earlier cost-history assumptions cannot coexist with the new ownership model.

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

Glow's `tariff-list` is treated as an effective-dated ledger rather than a set of constants. The integration stores the returned tariff history per commodity and refreshes it daily. It also stores the derived period analysis locally: resolved standing charge, flat unit rate where applicable, residual sample count/median/MAD and comparison with the configured current tariff.

That preserves historical standing-charge and unit-rate changes such as price-cap changes without recalculating old bills using today's tariff. Glow documents that DCC tariff history may be unavailable before the meter/account registration point; any leading period without tariff plan detail can only be inferred from the billing residual evidence available for that period.

The **P1D cost API remains authoritative for actual historical charges**. Tariff data is used to split and explain that total, never to rewrite the bill itself.

## Sensors

The integration creates:

- electricity and gas consumption sensors;
- electricity and gas API cost sensors;
- daily cost sensors;
- separate usage-cost and standing-charge sensors;
- combined daily standing-charge and total daily energy-cost sensors.

If a meter resource exists but Bright has no usable data for it, the corresponding entity remains **Unknown** rather than being forced to zero. This is particularly useful for a real meter that is temporarily not communicating.

Bright/DCC data is delayed rather than real-time, but Bright may publish current-day PT30M snapshots. Version 2.3 consumes those snapshots on the rolling schedule while still requiring a complete day before advancing the historical baseline.

## Real-data regression contract

The test suite includes a privacy-safe oracle derived from a contributed real Bright electricity export containing **19,346 half-hour readings** across 404 UK-local days. The raw household CSV is not stored in the repository.

The protected live test uses repository environment credentials and verifies:

- Glowmarkt `first-time` still locates the known historical neighbourhood;
- the PT30M readings endpoint resolves an actual first available interval near that locator;
- `last-time` has not regressed behind the known export;
- known completed days retain the exact expected half-hour timestamp geometry, including **50** intervals on the autumn DST day and **46** on the spring DST day;
- a known completed electricity-cost day has a positive P1D-minus-PT30M standing-charge residual;
- `tariff-list` returns effective-dated tariff history for the known electricity cost resource;
- a recent real tariff period has enough completed-day evidence for its residual cluster to centre plausibly on Glow's explicit standing charge, without printing the household tariff into Actions logs.

The contributed CSV is a historical snapshot, not an immutable billing ledger. Live testing proved that Glowmarkt can revise or remove old consumption values while preserving the same timestamp geometry and `first-time` metadata. The **current PT30M readings API is therefore authoritative for consumption values**; old CSV totals and value fingerprints are useful evidence, but they are not release gates.

The live job checks boundaries, timestamp geometry and cost semantics without printing raw household readings, tariff values or credentials to Actions logs. It is **manual-only** and runs separately from normal pull-request CI.

See [docs/TESTING.md](docs/TESTING.md) for the test strategy and live-contract rules.

## Development

Normal pull-request CI never uses Bright credentials. It runs mocked/unit tests, Ruff, Hassfest and HACS validation. The protected live contract is separate and uses the `glow-live` GitHub Environment.

Version 2.3 regression coverage explicitly includes:

- 46/48/50 interval day geometry;
- idempotent rolling PT30M ingestion;
- restart continuity;
- shorter/no open-day responses;
- completed-day-before-open-day ordering;
- incomplete closed-day blocking;
- provisional cost rebuilding;
- authoritative P1D reconciliation.

Bug fixes should arrive with a regression test. Historical/cumulative energy and cost changes require particular care around duplicate imports, restart behaviour, missing readings, UK-local day boundaries, DST and API revisions.

Repository/agent maintenance rules are documented in [AGENTS.md](AGENTS.md).

## Acknowledgements

This fork builds on the work of the original `xmcdanx` / `McDon22` Hildebrand Glow integration and its contributors. Operational patterns were also reviewed against `jonandel/ha-hildebrandglow-dcc`, including resource-based identity, separated polling concerns and API-protective polling intervals.

The project remains MIT licensed; see [LICENSE](LICENSE).
