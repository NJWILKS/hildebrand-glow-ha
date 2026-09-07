# Architecture

This document describes the design of the rescued Hildebrand Glow / Bright Home Assistant integration as of the 2.1.0 line.

## Design goals

The integration is designed around six priorities:

1. import all consumption history that Glowmarkt actually exposes;
2. preserve the real half-hour timestamps so Home Assistant's Energy dashboard shows the real historical shape rather than daily lumps;
3. avoid duplicate cumulative consumption across refreshes and Home Assistant restarts;
4. keep actual billing cost separate from explanatory tariff data;
5. expose historical usage-cost and standing-charge components in a form Home Assistant can graph natively;
6. be conservative with the Glowmarkt API through pacing, lower polling frequency and bounded retries.

## Source-of-truth hierarchy

Glowmarkt exposes metadata, readings, cost and tariff endpoints. They are deliberately not treated as interchangeable.

For the **start of consumption history**:

1. `GET /resource/{id}/first-time` is a locator only;
2. the integration queries a small `PT30M` readings window around that locator;
3. the earliest returned non-null reading is the authoritative first available billing interval;
4. a value of `0.0` is valid consumption data and must not be confused with a missing/null reading.

This rule exists because live validation showed that `first-time` can continue to reference historic intervals that the readings endpoint no longer returns.

For the **end of history**, `last-time` is useful as a diagnostic/contract boundary, but imported consumption still comes from the readings endpoint.

For **billing cost**:

- current and historical cost values from the Glow cost resource are authoritative;
- PT30M/hourly cost is usage-only;
- P1D cost is the authoritative completed-day total and includes standing charge;
- the standing-charge component is observed as `P1D - sum(PT30M)` for the same UK-local day;
- configured rate/standing-charge values are fallback inputs only when Glow cost data is unavailable.

For **tariffs**:

- `tariff-list` is treated as an effective-dated explanatory ledger;
- tariff history is persisted separately and refreshed daily;
- tariff records do not rewrite historical API cost values.

## Configuration and identity

A Bright account may expose more than one virtual entity/site. Setup therefore selects a specific Bright site and resource discovery is scoped to that site.

Identity is deliberately based on stable Glow data rather than Home Assistant's disposable config-entry ID wherever possible:

- config entries are site-specific;
- sensor identity prefers the Glow resource ID;
- cumulative history storage and Recorder lookup use the same stable identity model.

This reduces the chance that removing and re-adding the same Bright site creates a second logical meter or splits long-term statistics.

## API transport

All Glowmarkt requests for an account share a paced request lane.

The client:

- spaces requests rather than launching them concurrently;
- honours `Retry-After` where supplied;
- retries HTTP 429 and transient 5xx responses with bounded backoff;
- retries transient aiohttp connection failures/disconnects with bounded backoff;
- fails real authentication and non-transient API errors rather than silently converting them to empty data.

A transient failure must never be interpreted as "end of history" or "no standing charge".

## Polling model

Live polling and historical backfill serve different purposes and are deliberately separated.

Default live polling:

- consumption: 15 minutes;
- API-derived cost resources: 60 minutes.

Both are configurable, with a five-minute minimum.

The sensor platform does not perform a second first-refresh after the integration setup refresh. Historical consumption and cost work is scheduled in the background rather than blocking Home Assistant startup. The shared API request lane prevents those background tasks from bursting concurrent requests at Glowmarkt.

## Completed-day consumption model

DCC consumption is delayed and may arrive more than a day late. The integration therefore does not assume that "yesterday" is complete.

For normal updates it searches recent completed UK-local days and uses the latest day that contains actual PT30M readings.

The current UK-local day is not treated as a completed historical consumption day.

Day queries are filtered explicitly as a half-open interval:

`[day_start, next_day_start)`

This protects against Glowmarkt returning a bucket exactly on the query end boundary.

## Full consumption-history backfill

Once the real first available interval is resolved, the integration walks forward to the start of the current UK-local day.

History is fetched with `PT30M` readings in bounded multi-day chunks. The chunk size is deliberately below Glowmarkt's documented maximum so a 25-hour autumn DST day cannot cause the UTC request span to exceed the API limit.

Each returned timestamp is converted to `Europe/London` before grouping into a calendar day. This means:

- ordinary UK days contain 48 half-hour intervals;
- the spring DST day contains 46;
- the autumn DST day contains 50.

Missing/null rows are ignored. Genuine zero readings are preserved.

## Cost and standing-charge history

Cost history is reconstructed from the cost resource without recalculating bills from a tariff.

For each commodity:

1. the real first available cost interval is resolved;
2. PT30M cost is fetched in DST-safe bounded chunks and grouped by UK-local day;
3. P1D cost is fetched in bounded daily chunks and grouped by the same UK-local date;
4. completed days are joined;
5. usage cost is the PT30M sum;
6. total cost is the P1D value;
7. standing charge is the positive residual `P1D - PT30M` within tolerance.

If P1D is missing, usage cost remains usable but standing charge is `daily_pending`. If PT30M is missing or the residual is contradictory, the split is `unknown` instead of guessed.

The current partial day intentionally uses PT30M cost only, so its standing-charge component is zero until the daily aggregate exists.

## Home Assistant statistics

Consumption sensors use cumulative `TOTAL_INCREASING` semantics.

Historical PT30M consumption readings are converted into Home Assistant Recorder statistics using the original timestamps. Home Assistant therefore receives the historic hourly shape rather than one daily aggregate appearing in a single hour.

Historical cost components are imported into separate monetary `TOTAL` sensors:

- Electricity Usage Cost
- Electricity Standing Charge
- Gas Usage Cost
- Gas Standing Charge

Each historical day receives a Recorder statistic `state` for the component and a cumulative `sum`. This allows Home Assistant's native Statistics Graph card to render usage cost and standing charge as stacked bars using `chart_type: bar-stack` and `stat_types: state`.

The integration persists consumption cumulative/day state so:

- polling the same completed day again does not increment the cumulative total twice;
- a Home Assistant restart does not add the last completed day again;
- an older delayed day arriving after a newer day cannot corrupt the cumulative sequence.

Cost-history backfill has its own completion marker so it is not repeatedly re-imported on every reload.

## Tariff ledger

The tariff history returned by `tariff-list` is persisted per site and commodity as an effective-dated ledger. Records are sorted by their effective date (`effectiveDate` / `from`) and refreshed daily while the config entry is loaded.

This means price-cap or supplier tariff changes remain historically distinct instead of being flattened into the current setup values. The tariff ledger is explanatory metadata; actual historical cost continues to come from the cost resource.

The worker that maintains the tariff ledger is owned by the config entry and is cancelled when the integration unloads, preventing duplicate recurring workers after reloads.

## Backfill lifecycle

Consumption backfill is non-blocking and runs only after the corresponding sensor entities exist in Home Assistant. If setup timing or an API failure prevents a backfill attempt, the integration can retry rather than permanently marking backfill as started/completed.

Cost-history backfill waits for its synthetic monetary entities to be registered before importing statistics. Tariff history is refreshed after the initial delay and then daily.

## Failure philosophy

The integration distinguishes these states:

- **real reading**, including `0.0`;
- **no reading yet**, represented by missing/null data;
- **daily cost pending**, where PT30M usage cost exists but P1D has not appeared;
- **unknown component split**, where the API cannot safely separate usage from standing charge;
- **request failed**, represented by an exception/retry path.

Those states must never be collapsed together. In particular, a failed request cannot be used as evidence that no older data exists or that a standing charge was not applied.

## External influences

The rescue fork retains the original Hildebrand Glow integration lineage and incorporates lessons from its outstanding pull requests. Operational patterns were also reviewed against `jonandel/ha-hildebrandglow-dcc`, especially resource-based identity, reduced polling pressure and separation of polling concerns.

The historical Recorder-import model in this fork is intentionally different: the goal is to reconstruct all available DCC history with the original timestamps and cost components rather than expose daily-resetting sensors and require Utility Meter workarounds.
