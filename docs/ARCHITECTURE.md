# Architecture

This document describes the design of the rescued Hildebrand Glow / Bright Home Assistant integration as of the 2.0.0 line.

## Design goals

The integration is designed around four priorities:

1. import all consumption history that Glowmarkt actually exposes;
2. preserve the real half-hour timestamps so Home Assistant's Energy dashboard shows the real historical shape rather than daily lumps;
3. avoid duplicate cumulative consumption across refreshes and Home Assistant restarts;
4. be conservative with the Glowmarkt API through pacing, lower polling frequency and bounded retries.

## Source-of-truth hierarchy

Glowmarkt exposes both metadata and readings endpoints. They are not assumed to be perfectly consistent.

For the **start of history**:

1. `GET /resource/{id}/first-time` is a locator only;
2. the integration queries a small `PT30M` readings window around that locator;
3. the earliest returned non-null reading is the authoritative first available billing interval;
4. a value of `0.0` is valid consumption data and must not be confused with a missing/null reading.

This rule exists because live validation showed that `first-time` can continue to reference historic intervals that the readings endpoint no longer returns.

For the **end of history**, `last-time` is useful as a diagnostic/contract boundary, but imported consumption still comes from the readings endpoint.

## Configuration and identity

A Bright account may expose more than one virtual entity/site. Setup therefore selects a specific Bright site and resource discovery is scoped to that site.

Identity is deliberately based on stable Glow data rather than Home Assistant's disposable config-entry ID wherever possible:

- config entries are site-specific;
- sensor identity prefers the Glow resource ID;
- cumulative history storage and Recorder lookup use the same stable identity model.

This reduces the chance that removing and re-adding the same Bright site creates a second logical meter or splits long-term statistics.

## API transport

All Glowmarkt GET requests for an account share a paced request lane.

The client:

- spaces requests rather than launching them concurrently;
- honours `Retry-After` where supplied;
- retries HTTP 429 and transient 5xx responses with bounded backoff;
- retries transient aiohttp connection failures/disconnects with bounded backoff;
- fails real authentication and non-transient API errors rather than silently converting them to empty data.

A transient failure must never be interpreted as "end of history".

## Polling model

Live polling and historical backfill serve different purposes and are deliberately separated.

Default live polling:

- consumption: 15 minutes;
- API-derived cost resources: 60 minutes.

Both are configurable, with a five-minute minimum.

The sensor platform does not perform a second first-refresh after the integration setup refresh. Historical backfill is scheduled in the background rather than blocking Home Assistant startup.

## Completed-day model

DCC consumption is delayed and may arrive more than a day late. The integration therefore does not assume that "yesterday" is complete.

For normal updates it searches recent completed UK-local days and uses the latest day that contains actual PT30M readings.

The current UK-local day is not treated as a completed historical day.

Day queries are filtered explicitly as a half-open interval:

`[day_start, next_day_start)`

This protects against Glowmarkt returning a bucket exactly on the query end boundary.

## Full-history backfill

Once the real first available interval is resolved, the integration walks forward to the start of the current UK-local day.

History is fetched with `PT30M` readings in bounded multi-day chunks. The chunk size is deliberately below Glowmarkt's documented maximum so a 25-hour autumn DST day cannot cause the UTC request span to exceed the API limit.

Each returned timestamp is converted to `Europe/London` before grouping into a calendar day. This means:

- ordinary UK days contain 48 half-hour intervals;
- the spring DST day contains 46;
- the autumn DST day contains 50.

Missing/null rows are ignored. Genuine zero readings are preserved.

## Home Assistant statistics

Consumption sensors use cumulative `TOTAL_INCREASING` semantics.

Historical PT30M readings are converted into Home Assistant Recorder statistics using the original timestamps. Home Assistant therefore receives the historic hourly shape rather than one daily aggregate appearing in a single hour.

The integration persists cumulative/day state so:

- polling the same completed day again does not increment the cumulative total twice;
- a Home Assistant restart does not add the last completed day again;
- an older delayed day arriving after a newer day cannot corrupt the cumulative sequence.

## Backfill lifecycle

Backfill is non-blocking and runs only after the corresponding sensor entities exist in Home Assistant. If setup timing or an API failure prevents a backfill attempt, the integration can retry rather than permanently marking backfill as started/completed.

## Failure philosophy

The integration distinguishes three states:

- **real reading**, including `0.0`;
- **no reading yet**, represented by missing/null data;
- **request failed**, represented by an exception/retry path.

Those states must never be collapsed together. In particular, a failed request cannot be used as evidence that no older data exists.

## External influences

The rescue fork retains the original Hildebrand Glow integration lineage and incorporates lessons from its outstanding pull requests. Operational patterns were also reviewed against `jonandel/ha-hildebrandglow-dcc`, especially resource-based identity, reduced polling pressure and separation of polling concerns.

The historical Recorder-import model in this fork is intentionally different: the goal is to reconstruct all available DCC history with the original timestamps rather than expose a daily-resetting sensor and require a Utility Meter workaround.
