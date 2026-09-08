# Home Assistant Energy dashboard

Version 2.3.4 gives Hildebrand one statistics owner for Energy consumption and one tariff-first model for cost composition.

## Built-in Energy dashboard

Configure **Settings → Dashboards → Energy → Electricity grid** with:

- **Energy imported from grid:** the external statistic named **Hildebrand Glow Electricity Energy Consumption**.
- **Cost tracking:** the external statistic named **Hildebrand Glow Electricity Energy Cost**.

Upgrading from an earlier 2.3.x release migrates an existing Hildebrand Energy source from the old live-sensor statistic to the stable external consumption statistic automatically. The visible `Electricity Consumption Today` sensor is deliberately presentation-only and is not a second long-term statistics writer.

Do not use `Total Daily Energy Cost` for the electricity grid. That entity can combine electricity and gas when both commodities are available.

## Cost model

The integration first retrieves Glow's effective-dated `tariff-list` ledger.

For a flat tariff, each PT30M consumption interval is priced with the unit rate effective for that date:

`usage cost = Σ(PT30M kWh × effective unit rate)`

The effective standing charge is then applied once for the UK-local billing day:

`total cost = usage cost + standing charge`

For TOU or dynamic tariffs, where one flat unit rate cannot price the day correctly, Glow PT30M cost remains the usage-cost source and the effective tariff standing charge is still applied once.

Glow P1D cost is retained as a reconciliation check for completed days. A material difference between the independently priced components and Glow P1D is logged and is also covered by the protected live-contract release test.

The dedicated Energy total-cost statistic uses the same usage + standing composition as the stacked component chart. Its cumulative `sum` is Home Assistant plumbing; the user-facing values remain the dated hourly/daily costs.

The current day can include the standing charge immediately once the effective tariff is known; it no longer has to wait for Glow's next-day P1D aggregate before the stacked chart can show the fixed daily charge.

## Reset imported history

Use **Settings → Devices & services → Hildebrand Glow → Configure → Reset imported history**.

The reset is scoped to the selected Hildebrand meter site. It:

- clears the integration-owned historical statistics;
- clears the dedicated Hildebrand electricity/gas Energy statistics;
- removes the integration's consumption, cost and tariff-ledger cache files;
- preserves credentials, entity registry entries, dashboard configuration and unrelated Home Assistant history;
- reloads the config entry so consumption, tariff and cost history are rebuilt deterministically from Glow.

The stable external statistic IDs mean an existing Energy dashboard can be migrated rather than manually rebuilt.

## Usage cost versus standing charge graph

The two component statistics are attached to the normal Hildebrand entities:

- `Smart Meter Electricity Usage Cost`
- `Smart Meter Electricity Standing Charge`

Both historical and current-day statistics are imported by the integration. The live entities deliberately do not carry a Recorder state class, which prevents Home Assistant from creating a competing second statistics series.

Example native stacked card:

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

Each daily bar therefore reads as:

- lower segment: actual usage cost at the effective tariff rate;
- upper segment: that day's effective standing charge;
- full bar height: the same daily total used by the dedicated Energy cost statistic.

Home Assistant may choose different entity IDs if similarly named entities already existed; use the entity picker or the actual registry IDs in YAML.

## Gas

Gas follows the same model when usable gas data is available: external consumption + external total-cost statistics for the Energy dashboard, and separate usage/standing component entities for stacked analysis. If a DCC/Glow gas resource exists but has no readings, the values remain unavailable rather than being silently treated as zero.
