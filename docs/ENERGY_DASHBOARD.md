# Home Assistant Energy dashboard

The Hildebrand Glow integration supplies two related historical cost views because Home Assistant uses them for different purposes.

## Built-in Energy dashboard

Configure **Settings → Dashboards → Energy → Electricity grid** with:

- **Energy imported from grid:** `Smart Meter Electricity Consumption`
- **Cost tracking:** select the dedicated Hildebrand Glow electricity cost statistic created by the integration. In Home Assistant it is named **Hildebrand Glow Electricity Energy Cost**.

Do not use `Total Daily Energy Cost` for the electricity grid. That entity combines electricity and gas when both commodities are available and would therefore attribute gas cost to electricity.

Do not rely on `Electricity Daily Cost` as the Energy dashboard's historical cost source. It remains a useful human-facing sensor for the latest/current billing day, but version 2.1.2 and later publish a dedicated cumulative external statistic for Energy-dashboard billing history. This matches Home Assistant's own delayed-billing integrations such as Opower.

The dedicated statistic is backfilled with:

- the authoritative completed P1D Glow cost for each UK-local day;
- the original PT30M usage-cost shape where available;
- the P1D-minus-PT30M residual folded into the first hour so the complete day's statistic still equals the Glow P1D bill;
- a cumulative `sum` column, which is the value Home Assistant's Energy dashboard uses for historical cost.

Recorder keeps sub-penny precision for imported cost data. The UI may still display normal currency rounding, but that display formatting no longer changes the accumulated billing statistic.

Version 2.1.5 rebuilds the integration-owned external Energy cost statistic once on upgrade. This removes stale tail rows left by earlier 2.1.x cost backfills that could make Home Assistant display a large negative cost. After that migration, incremental updates resume from Recorder's actual last persisted external `sum`, rather than trusting the integration's sidecar cache as the billing arithmetic authority.

Cost history is then extended incrementally every six hours. A trailing completed day is not finalised until Glow has published its P1D bucket, so the integration does not accidentally treat a usage-only PT30M total as the final bill.

For the current partial day, Glow PT30M cost is usage-only. The standing charge appears only after Glow publishes the completed P1D bucket.

## Reset imported history

Version 2.2.0 adds a maintenance action under **Settings → Devices & services → Hildebrand Glow → Configure → Reset imported history**.

The reset is deliberately scoped to the selected Hildebrand meter site. It:

- clears Recorder statistics for the integration's sensor entities;
- clears the dedicated Hildebrand electricity/gas Energy cost statistics;
- removes the integration's cumulative-history, cost-history and tariff-ledger cache files;
- preserves credentials, entity registry entries, dashboard configuration and all unrelated Home Assistant history;
- reloads the config entry so consumption, cost and tariff history are rebuilt cleanly from Glow.

A confirmation screen is shown before any statistics are deleted. Because the statistic IDs remain stable, the Energy dashboard does not need to be reconfigured after a successful reset; it will repopulate as the backfill completes.

## Usage cost versus standing charge graph

The integration also imports separate historical statistics for:

- `Smart Meter Electricity Usage Cost`
- `Smart Meter Electricity Standing Charge`

These are intended for analysis and native stacked graphs rather than as the Energy dashboard's total-cost source.

Example native card:

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

Home Assistant may choose different entity IDs if the names already existed; use the entity picker if necessary.

## Gas

When usable gas data is available, configure the Energy dashboard gas source with `Smart Meter Gas Consumption` and the dedicated **Hildebrand Glow Gas Energy Cost** statistic using the same pattern. If the DCC/Glow gas resource exists but currently has no readings, the gas entities remain `Unknown` rather than being treated as zero.
