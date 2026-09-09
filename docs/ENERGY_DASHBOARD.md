# Home Assistant Energy dashboard

Version 2.3.6 gives Hildebrand one statistics owner for Energy consumption, one external total-cost source for the native Energy dashboard, and separate external usage/standing statistics for stacked analysis.

## Built-in Energy dashboard

Configure **Settings → Dashboards → Energy → Electricity grid** with:

- **Energy imported from grid:** the external statistic named **Hildebrand Glow Electricity Energy Consumption**.
- **Cost tracking:** the external statistic named **Hildebrand Glow Electricity Energy Cost**.

Upgrading from an earlier 2.3.x release migrates an existing Hildebrand Energy source from the old live-sensor statistic to the stable external consumption statistic automatically. Version 2.3.6 also repairs stale fixed/entity price fields left behind on an already-external Hildebrand source and binds the matching external total-cost statistic when no explicit alternative cost statistic is selected.

The visible `Electricity Consumption Today` sensor is deliberately presentation-only and is not a second long-term statistics writer.

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
- clears the external usage-cost and standing-charge component statistics;
- removes the integration's consumption, cost and tariff-ledger cache files;
- preserves credentials, entity registry entries, dashboard configuration and unrelated Home Assistant history;
- reloads the config entry so consumption, tariff and cost history are rebuilt deterministically from Glow.

The stable external statistic IDs mean an existing Energy dashboard can be migrated rather than manually rebuilt.

## Usage cost versus standing charge graph

Version 2.3.6 publishes the two chart components as integration-owned external statistics rather than attaching long-term history to visible sensor entities:

- `hildebrand_glow:<site>_electricity_usage_cost`
- `hildebrand_glow:<site>_electricity_standing_charge`

The visible Usage Cost / Standing Charge entities remain useful current-day presentation values, but historical charting should use the external statistic IDs above. This avoids Recorder becoming a competing statistics owner and also avoids orphaned-entity history confusing the chart.

Example native stacked card:

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

Use the exact external statistic IDs shown in **Developer Tools → Statistics** for the selected site.

`change` is used deliberately. Both external component statistics maintain a monotonic internal `sum`; the daily change is therefore exactly that day's usage cost or standing charge.

Each daily bar reads as:

- lower segment: actual usage cost at the effective tariff rate;
- upper segment: that day's effective standing charge;
- full bar height: the same daily total used by the dedicated Energy cost statistic.

## Gas

Gas follows the same model when usable gas data is available: external consumption + external total-cost statistics for the Energy dashboard, plus external usage-cost and standing-charge component statistics for stacked analysis. If a DCC/Glow gas resource exists but has no readings, the values remain unavailable rather than being silently treated as zero.
