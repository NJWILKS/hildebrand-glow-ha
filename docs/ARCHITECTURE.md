# Architecture

This document describes the Hildebrand Glow / Bright Home Assistant integration as of version 2.3.4.

## Design goals

The integration is designed to:

1. import all consumption history that Glow actually exposes;
2. preserve real PT30M timestamps and UK-local day geometry;
3. update the current day as Bright publishes new intervals;
4. keep one statistics owner so reload/reset/backfill cannot create fake meter resets;
5. price historical/current cost from the tariff actually effective on the date;
6. expose useful date-contextual values to the user rather than a synthetic lifetime register;
7. remain conservative with the Glow API through pacing, retries and bounded requests.

## Source-of-truth hierarchy

### Consumption

- `first-time` is a locator only.
- PT30M readings are authoritative interval energy.
- A real `0.0` interval is valid data; missing/null is not.
- The integration writes one dedicated external Home Assistant Energy statistic for each commodity.
- The visible consumption entity shows today's published usage and has no `TOTAL_INCREASING` state class.

### Tariffs

- `tariff-list` is an effective-dated ledger.
- A single explicit rate is a flat tariff even when Glow includes a `tier: 1` marker.
- Time windows / explicit TOU rate structure identify time-of-use tariffs.
- Dynamic tariff markers identify dynamic tariffs.
- Multiple tiered rates without time windows are treated as block tariffs, not flattened to one rate.

### Cost

- For flat tariffs, PT30M consumption × the effective unit rate is authoritative for usage cost.
- The effective standing charge is applied exactly once per UK-local billing day, including the open day when the tariff is known.
- For TOU/dynamic tariffs, Glow PT30M cost remains the authoritative usage-cost shape because one flat rate cannot price the day correctly.
- Glow P1D cost is a completed-day reconciliation oracle. It must remain close to Usage + Standing but does not redefine those components when an explicit tariff can price them directly.

## Statistics ownership

Version 2.3.4 removes the previous mixed ownership model.

For each commodity the integration owns an external Energy statistic, for example:

`hildebrand_glow:<site>_electricity_energy_consumption`

The external statistic uses:

- `state`: the dated interval/hour quantity in kWh;
- `sum`: the monotonic cumulative value Home Assistant requires internally.

The cumulative `sum` is bookkeeping, not a user-facing physical meter register.

The visible **Electricity Consumption Today** and **Gas Consumption Today** entities have no long-term state class. Recorder therefore cannot create a competing consumption statistic from them.

Historical and current-day component cost statistics are likewise integration-owned where imported statistics are required.

## Fresh install / schema rebuild

On a fresh install or a consumption schema rebuild:

1. discover the selected site's resources;
2. use `first-time` as an approximate historical locator;
3. resolve the first actual available PT30M reading;
4. retrieve the available history in bounded chunks;
5. clear legacy mixed-ownership consumption statistics plus the owned external IDs for this site;
6. rebuild the external series deterministically;
7. migrate Home Assistant Energy configuration from the legacy sensor statistic ID to the external statistic ID where applicable;
8. continue writing the same external series for the current day.

Reset/rebuild must be idempotent: repeating it from the same Glow data produces the same statistics.

## Rolling current-day consumption

Current-day data is recalculated from source on each poll.

The integration fetches today's published PT30M intervals, discards intervals whose half hour has not yet closed, and publishes them into the same external statistic used for history.

Repeating an identical response is idempotent.

If Bright temporarily returns fewer intervals than were previously published for the same day, the integration preserves the last good external Energy rows rather than allowing the internal cumulative sum to move backwards.

The visible sensor value is the current day's PT30M total; the external statistic's internal cumulative sum is not exposed as that sensor value.

## Completed-day reconciliation

Closed days are processed sequentially. A day is complete only when the PT30M timestamp geometry matches the UK-local day:

- ordinary day: 48 intervals;
- spring DST transition: 46 intervals;
- autumn DST transition: 50 intervals.

The expected count is derived from the UTC span between consecutive Europe/London midnights.

If the next closed day is incomplete, reconciliation stops there. Later days are not skipped because doing so would make the cumulative baseline ambiguous.

## Cost ingestion

Cost ingestion has one worker per config entry.

The worker:

1. retrieves Glow cost history;
2. retrieves matching consumption history;
3. retrieves/persists effective-dated tariff history;
4. derives tariff periods;
5. prices flat-tariff usage from consumption × rate;
6. retains Glow PT30M usage cost for tariffs that cannot be represented by one rate;
7. applies the effective standing charge once per UK-local day;
8. imports Usage Cost, Standing Charge and total-cost statistics;
9. reconciles completed-day totals against Glow P1D and logs material disagreement.

This ordering matters: tariff information must be available before historical cost reconciliation.

## Tariff classification

Glow plan detail is structural metadata, not a direct tariff-type flag.

Classification rules are therefore conservative:

- dynamic marker present -> dynamic;
- time window or explicit TOU rate present -> TOU;
- exactly one unique rate -> flat, even with `tier: 1`;
- multiple tiered rates without time windows -> block;
- otherwise -> unknown.

Unknown is not silently flattened. The protected live contract should fail if the known live tariff can no longer be classified safely.

## Identity

A Bright account can expose multiple virtual entities/sites. Setup selects a specific site.

Identity prefers stable Glow resource/site information over disposable Home Assistant config-entry identifiers where possible, so removing/re-adding a site does not unnecessarily create a second logical meter or split statistics.

## API transport

All Glow requests share a paced request lane.

The client:

- spaces requests rather than flooding the API;
- honours `Retry-After` where supplied;
- retries HTTP 429 and transient 5xx responses with bounded backoff;
- retries transient connection failures;
- never converts a request failure into zero consumption, an empty-history boundary or proof that no tariff/standing charge exists.

## Lifecycle

Exactly one consumption-history worker and one cost-ingestion worker may exist per config entry.

Config-entry unload cancels integration-owned tasks cleanly. Reset/reload must not leave an older worker writing into Recorder while a new worker rebuilds the same statistics.

## Failure philosophy

The implementation distinguishes:

- real zero data;
- missing data;
- incomplete closed day;
- shorter transient open-day response;
- unavailable tariff detail;
- unknown tariff structure;
- request failure.

Those states are never collapsed together.

## Regression contract

Ordinary CI uses no Bright credentials and covers the deterministic/statistical contracts with pytest, Ruff, Hassfest and HACS validation.

A separate protected live contract validates API semantics against a real Bright account, including DST geometry, tariff shape/classification, standing-charge evidence and flat-rate consumption × tariff pricing.

The release-specific gates are documented in `docs/RELEASE_2_3_4_AUDIT.md`; the low-level statistics invariants are documented in `docs/STATISTICS_CONTRACT.md`.
