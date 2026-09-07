# Home Assistant Energy dashboard

The Hildebrand Glow integration supplies two different historical views of electricity cost because Home Assistant uses them for different purposes.

## Built-in Energy dashboard

Configure **Settings → Dashboards → Energy → Electricity grid** with:

- **Energy imported from grid:** `Smart Meter Electricity Consumption`
- **Cost tracking:** **Use an entity tracking the total costs**
- **Total-cost entity:** `Smart Meter Electricity Daily Cost`

Do not use `Total Daily Energy Cost` for the electricity grid. That entity combines electricity and gas when both commodities are available and would therefore attribute gas cost to electricity.

`Electricity Daily Cost` is a monetary `TOTAL` sensor with a UK-local daily reset. Version 2.1.1 and later backfill its Recorder statistics with:

- the authoritative completed P1D Glow cost for each UK-local day;
- the original PT30M usage-cost shape where available;
- the P1D-minus-PT30M residual folded into the first hour so the complete day's statistic still equals the Glow P1D bill;
- a cumulative `sum` column, which is the value Home Assistant's Energy dashboard uses for historical cost.

On upgrade from 2.1.0, the cost-history backfill schema is advanced automatically so existing installations receive the missing Energy-dashboard cost statistics without deleting entities or Recorder history.

For the current partial day, Glow PT30M cost is usage-only. The standing charge appears only after Glow publishes the completed P1D bucket.

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

When usable gas data is available, configure the Energy dashboard gas source with `Smart Meter Gas Consumption` and `Smart Meter Gas Daily Cost` using the same pattern. If the DCC/Glow gas resource exists but currently has no readings, the gas entities remain `Unknown` rather than being treated as zero.
