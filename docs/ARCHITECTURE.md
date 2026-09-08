# Architecture

This document describes the Hildebrand Glow / Bright Home Assistant integration as of the 2.3.0 line.

## Design goals

The integration is designed around seven priorities:

1. import all consumption history that Glowmarkt actually exposes;
2. preserve the real half-hour timestamps so Home Assistant's Energy dashboard shows the real historical shape rather than daily lumps;
3. update the current day as Bright publishes new PT30M snapshots;
4. avoid duplicate or backwards cumulative consumption across polling, API revisions and Home Assistant restarts;
5. keep actual billing cost separate from explanatory tariff data;
6. reconcile completed-day usage and standing charge against authoritative Glow P1D cost;
7. be conservative with the Glowmarkt API through pacing, lower polling frequency and bounded retries.

## Source-of-truth hierarchy

Glowmarkt exposes metadata, readings, cost and tariff endpoints. They are deliberately not treated as interchangeable.

For the **start of consumption history**:

1. `GET /resource/{id}/first-time` is a locator only;
2. the integration queries a small `PT30M` readings window around that locator;
3. the earliest returned non-null reading is the authoritative first available billing interval;
4. a value of `0.0` is valid consumption data and must not be confused with a missing/null reading.

This rule exists because live validation showed that `first-time` can continue to reference historic intervals that the readings endpoint no longer returns.

For the **end of history**, `last-time` is useful as a diagnostic/contract boundary, but imported consumption still comes from the readings endpoint.

For **consumption**:

- PT30M readings are authoritative;
- closed historical days are imported into Recorder statistics by the integration;
- the current UK-local day is represented by the live cumulative sensor, calculated from the completed historical baseline plus all currently published PT30M intervals for today;
- the integration never manually imports current-day consumption statistics, so Recorder has only one writer for the open day.

For **billing cost**:

- PT30M/hourly cost is usage-only;
- P1D cost is the authoritative completed-day total and includes standing charge;
- the current open day uses the published PT30M usage cost only;
- once P1D exists, the completed day is reconciled to that authoritative total;
- configured rate/standing-charge values are calibration/fallback inputs only when Glow data is incomplete or unavailable.

For **tariffs**:

- `tariff-list` is treated as an effective-dated explanatory ledger;
- tariff history is persisted separately and refreshed daily;
- tariff records explain and normalise component splits but never rewrite the authoritative P1D bill total.

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

A transient failure must never be interpreted as "end of history", "zero consumption" or "no standing charge".

## Polling model

Live polling and historical reconciliation serve different purposes but share the same source-of-truth rules.

Default polling:

- consumption: 15 minutes;
- API-derived cost resources: 60 minutes.

Both are configurable, with a five-minute minimum.

Every consumption poll works in this order:

1. load the completed historical cumulative baseline;
2. reconcile every closed but incomplete/unreconciled UK-local day in sequence;
3. stop if the next closed day does not yet contain the DST-correct full PT30M geometry;
4. when the completed baseline reaches yesterday, fetch today's published PT30M intervals;
5. expose `completed baseline + today's PT30M sum` as the live `TOTAL_INCREASING` sensor state.

This order is important. A machine that was off for several days repairs those completed days before it resumes the current day, so a restart or outage cannot double count or jump over a missing day.

The cost path follows the same boundary:

1. reconcile all available completed P1D days;
2. establish the authoritative completed cost baseline;
3. publish today's PT30M usage cost provisionally;
4. replace/reconcile that provisional day once Glow publishes P1D.

The sensor platform does not perform a second first-refresh after integration setup. Historical work is started in background tasks owned by the config entry and those tasks are cancelled on unload/reset.

## Rolling current-day consumption

The current UK-local day is deliberately **not** written with `async_import_statistics`. Home Assistant Recorder records the live `TOTAL_INCREASING` sensor naturally as its cumulative state rises.

On each poll the current-day value is recomputed from source rather than incrementally adding a delta:

`completed_cumulative + sum(all currently published PT30M intervals today)`

This makes polling idempotent. Replaying the same Bright response does not add the intervals twice, and a restart reconstructs the same state from the same completed baseline plus the same current-day snapshots.

Bright can temporarily return a shorter current-day window. If a response contains fewer PT30M intervals than the last good response for the same day, the integration holds the last good cumulative value instead of moving a `TOTAL_INCREASING` sensor backwards and causing Home Assistant to interpret the change as a meter reset. A response with the same interval count is still accepted, allowing Bright to revise values legitimately.

Only closed PT30M intervals are included. An interval is considered usable once its 30-minute period has elapsed.

## Completed-day consumption reconciliation

A closed UK-local day is not marked complete merely because some data exists. The PT30M timestamp geometry must be complete:

- ordinary UK day: **48** intervals;
- spring DST transition: **46** intervals;
- autumn DST transition: **50** intervals.

The expected count is calculated from the UTC span between consecutive `Europe/London` midnights, so DST is data-driven rather than hard-coded by date.

If the next closed day is incomplete, reconciliation stops at that day. Later dates are not skipped because doing so would make the cumulative baseline ambiguous.

Day queries are treated as half-open ranges:

`[day_start, next_day_start)`

This protects against Glowmarkt returning a bucket exactly on the request end boundary.

## Full consumption-history backfill

On a fresh install or history reset the integration resolves the real first available interval and walks forward to the start of the current UK-local day.

History is fetched with PT30M readings in bounded multi-day chunks. Each timestamp is converted to `Europe/London` before grouping into a calendar day. Complete closed days are imported into Recorder as hourly cumulative statistics while preserving the original half-hour shape.

The open day is never included in this imported historical series. Once backfill completes, a normal coordinator refresh supplies the current-day live value through the sensor path.

Version 2.3 advances the consumption-history schema. During migration the integration clears its own legacy consumption statistic series once and rebuilds the completed historical series, removing earlier synthetic/current-day rows that could collide with Recorder's live statistics.

## Cost and standing-charge ingestion

Cost has a single ingestion owner with an explicit completed/open-day boundary.

For each commodity:

1. PT30M cost is fetched and grouped by UK-local day;
2. P1D cost is fetched and grouped by the same day;
3. every available completed day is reconciled before the current day is published;
4. the store records `last_completed_day` and completed cumulative cost baselines;
5. today's PT30M cost is published provisionally only when yesterday has been reconciled;
6. once today's P1D appears on a later poll, the same day becomes completed and the provisional values are replaced by the authoritative reconciliation.

P1D is authoritative for the completed day's total bill. The tariff ledger is then used to resolve a stable standing-charge value for the applicable tariff period. Daily usage cost is calculated as:

`P1D total - resolved standing charge`

This guarantees that usage plus standing charge equals the authoritative P1D bill while avoiding a noisy day-by-day standing-charge series caused by aggregation residuals.

If P1D is not available yet, the day remains pending. The open-day standing-charge sensor reports zero because Bright's PT30M cost does not contain standing charge; the charge is applied only when the completed P1D bucket exists.

## Home Assistant statistics ownership

Consumption sensors use cumulative `TOTAL_INCREASING` semantics.

Ownership is intentionally split by time boundary:

- **closed consumption history**: integration-owned Recorder imports;
- **open-day consumption**: Recorder records the live sensor, with no manual current-day imports;
- **Energy-dashboard total cost**: integration-owned external cost statistic;
- **usage/standing component sensors**: monetary `TOTAL` sensors whose historical statistics use the same daily-reset/hourly semantics as their live states.

The explicit ownership boundary prevents two mechanisms writing incompatible values into the same current-day statistic.

## Tariff ledger

The tariff history returned by `tariff-list` is persisted per site and commodity as an effective-dated ledger. Records are sorted by their effective date (`effectiveDate` / `from`) and refreshed daily while the config entry is loaded.

A tariff period prefers an explicit standing charge from Glow. If it is absent, the integration can infer a stable value from the median positive P1D-minus-PT30M residuals within that exact tariff period. The configured current tariff is used only as a calibration/fallback anchor where appropriate.

This means price-cap or supplier tariff changes remain historically distinct instead of being flattened into the current setup values. Actual historical total cost continues to come from P1D.

## Reset and backfill lifecycle

`Reset imported history` clears only statistics and stores owned by the selected Hildebrand config entry. Credentials, entity registry entries, dashboards and unrelated Recorder history are preserved.

After the reset, the normal setup/ingestion lifecycle rebuilds the data; reset does not use a special alternative algorithm. That is deliberate: a clean reset and an ordinary fresh installation must converge on the same result.

Background history tasks are owned by the config entry and cancelled during unload so a reset/reload cannot leave an old worker writing to Recorder while a new worker is rebuilding the same series.

## Failure philosophy

The integration distinguishes these states:

- **real reading**, including `0.0`;
- **no reading yet**, represented by missing/null data;
- **incomplete closed day**, which blocks the cumulative reconciliation boundary;
- **shorter transient open-day response**, which retains the last good live value;
- **daily cost pending**, where PT30M usage cost exists but P1D has not appeared;
- **unknown component split**, where the API cannot safely separate usage from standing charge;
- **request failed**, represented by an exception/retry path.

Those states must never be collapsed together. In particular, a failed or shorter request cannot be treated as zero consumption, a meter reset, end of history or proof that no standing charge applies.

## External influences

The rescue fork retains the original Hildebrand Glow integration lineage and incorporates lessons from its outstanding pull requests. Operational patterns were also reviewed against `jonandel/ha-hildebrandglow-dcc`, especially resource-based identity, reduced polling pressure and separation of polling concerns.

The historical Recorder-import model in this fork remains intentionally different: the aim is to reconstruct all available DCC history while also making the current day roll forward naturally as Bright publishes PT30M snapshots, without Utility Meter workarounds or duplicate current-day writers.
