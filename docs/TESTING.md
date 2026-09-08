# Testing strategy

The project uses two deliberately separate test layers: hermetic pull-request CI and a protected live Glowmarkt contract.

## Normal CI

Normal CI must never call the live Glowmarkt API and must never require Bright credentials.

Every pull request runs:

- pytest with `pytest-homeassistant-custom-component`;
- Ruff;
- Hassfest;
- HACS validation.

The standard test suite covers API parsing, config flow, multi-site selection, sensor metadata, identity, coordinator behaviour, cumulative/restart safety, DST handling, history chunking, rolling PT30M ingestion, cost aggregation semantics, tariff ordering, backoff and failure behaviour.

Run locally with:

```bash
python -m pip install -r requirements-dev.txt
pytest
ruff check .
```

## Regression-test rule

A behavioural bug fix should include a regression test that describes the failure independently of the implementation.

For historical and rolling energy/cost changes, tests should explicitly consider:

- missing/null readings;
- genuine zero readings;
- delayed DCC data;
- repeated polling with the same PT30M window;
- a current-day window that grows by one or more intervals;
- a transient current-day response that is shorter or empty;
- same-count API revisions to existing intervals;
- Home Assistant restart/reload during an open day;
- one or more missed closed days after downtime;
- incomplete closed days blocking later reconciliation;
- UK-local midnight boundaries;
- ordinary days with 48 PT30M intervals;
- spring DST with 46 PT30M intervals;
- autumn DST with 50 PT30M intervals;
- query end-boundary buckets;
- current-day consumption being live-sensor-owned rather than manually imported;
- PT30M cost being usage-only;
- P1D cost containing standing charge;
- closed-day P1D reconciliation happening before current-day cost publication;
- provisional open-day cost being replaceable by completed-day P1D;
- P1D not yet available (`daily_pending`);
- negative/contradictory residuals returning `unknown` rather than a negative charge;
- effective-dated tariff ordering;
- 429/5xx responses;
- transient connection drops;
- interruption during background backfill;
- unload/reset cancellation so an old worker cannot keep writing statistics.

## Consumption statistics ownership

Version 2.3 deliberately gives current and historical consumption different writers:

- completed history is imported by the integration using cumulative `TOTAL_INCREASING` statistics;
- the current UK-local day is **not** manually imported;
- Recorder observes the live cumulative sensor as `completed baseline + today's published PT30M total`.

Tests therefore verify that:

- a repeated open-day poll is idempotent;
- a restart reconstructs the same live cumulative value rather than adding the day twice;
- a shorter/no open-day response never moves the cumulative sensor backwards;
- a same-count revision is accepted;
- a completed gap is reconciled before the current day is exposed;
- an incomplete closed day holds the previous good state;
- migration to the 2.3 schema clears/rebuilds completed history without creating synthetic current-day Recorder rows.

## Cost statistics ownership

The dedicated Energy-dashboard cost statistic is integration-owned.

Tests verify the two-stage lifecycle:

1. while a day is open, available PT30M usage-cost intervals can be published provisionally;
2. once P1D appears, that day is reconciled to the authoritative completed total before a new open day is published.

The cost-history store records an explicit `last_completed_day`; the latest external Recorder row must never be treated as proof that a day is complete because the current day can also contain provisional rows.

## Cost-component statistics

Usage Cost and Standing Charge are separate monetary `TOTAL` sensors.

Historical component tests verify that:

- usage and standing-charge values reconcile to the authoritative P1D total for each completed day;
- their imported statistics follow the same hourly/daily-reset state semantics as the live sensors, rather than mixing a single daily historical state with hourly live states;
- the running statistic `sum` remains cumulative for Recorder compatibility;
- UK-local timestamps, including DST boundaries, map to the correct UTC statistic starts;
- an unknown standing-charge split does not invent a historical value.

These statistics are designed for Home Assistant's native Statistics Graph card with `chart_type: bar-stack` and `stat_types: state`.

## Tariff ledger

`tariff-list` is treated as an effective-dated ledger. Tests cover stable ordering by `effectiveDate` / `from`, effective-period standing-charge normalisation and the rule that tariff data explains the component split without replacing API-derived P1D billing totals.

The configured current tariff is a calibration/fallback anchor, not a mechanism for recalculating old bills.

## Reset contract

`Reset imported history` is intentionally narrow. Tests verify that reset targets only:

- this config entry's Hildebrand sensor statistics;
- this site's dedicated external Energy cost statistics;
- the integration's cumulative, cost-history and tariff-history stores.

It must not delete credentials, entity registry entries, dashboards or unrelated Recorder statistics.

After reset, the normal setup/ingestion path rebuilds history. There is no separate reset-only ingestion algorithm.

## Real-data oracle

A privacy-safe oracle is derived from a contributed real Bright electricity export.

The repository stores summary/fingerprint data only, not the raw household CSV. The oracle includes useful structural facts such as row counts, date boundaries and selected timestamp fingerprints.

Historical totals/fingerprints describe the export **at the time it was taken**. They are useful diagnostic evidence, but they are not assumed to be an immutable billing ledger: Glowmarkt can revise historic values later.

The raw household data must not be committed to this public repository.

## Protected live contract

The live contract exists to catch API behaviour that mocks cannot reveal.

It is manual-only and uses the GitHub Environment `glow-live` with:

- `GLOWMARKT_USERNAME`
- `GLOWMARKT_PASSWORD`

Credentials must be supplied as GitHub Environment secrets. They must never appear in source, fixtures, command-line arguments or logs.

The live test runs outside the Home Assistant pytest network sandbox because it intentionally calls the external API. Normal tests remain network-blocked.

The live contract verifies:

1. authentication and resource discovery against the real account;
2. `first-time` still locates the known start neighbourhood;
3. PT30M readings resolve the real first available interval near that locator;
4. `last-time` has not regressed behind the known export;
5. selected known historic days retain the exact expected PT30M timestamp geometry;
6. both UK DST transition days retain the correct geometry: 46 spring intervals and 50 autumn intervals;
7. a known completed cost day exposes PT30M cost and a larger P1D cost, proving a positive standing-charge residual;
8. `tariff-list` returns effective-dated tariff history for the known electricity cost resource.

The **current PT30M readings endpoint is authoritative for consumption values**. Exact old CSV totals or value hashes do not fail a release if the current API has revised them. The live contract is deliberately strict about boundaries, interval presence/order, DST geometry and aggregation semantics instead.

The **current Glow cost endpoint is authoritative for billing values**. Tests use tariff history to explain tariff changes, not to overwrite or reconstruct the API's historical bill.

The earliest exported day is also not treated as immutable because live testing demonstrated that very old Glowmarkt rows can disappear while the `first-time` metadata remains unchanged.

## Privacy rules for live logs

Live tests may report bounded diagnostics such as:

- test-case date;
- interval count;
- missing/extra timestamps;
- whether the P1D/PT30M relationship is valid;
- pass/fail state.

They must not print:

- Bright username/password;
- tokens;
- resource IDs where avoidable;
- addresses/site names;
- full raw readings;
- tariff values;
- full household consumption or cost exports.

## When to run the live contract

Run it manually when a change affects:

- authentication;
- resource discovery;
- readings query parameters;
- first/last history discovery;
- rolling current-day PT30M retrieval;
- completed-day reconciliation;
- cost aggregation periods;
- standing-charge reconciliation;
- tariff-list retrieval;
- retry/backoff transport;
- DST/day-boundary handling;
- historical backfill/import semantics.

Do not trigger it on every branch push. Normal CI is the default gate; the live contract is an explicit pre-release/acceptance gate.

## Release acceptance

Before merging a release-affecting PR:

1. Tests, Ruff, Hassfest and HACS must be green on the final head.
2. Relevant regression tests must exist.
3. If the API/history path changed, the protected live contract must pass.
4. Documentation must match the shipped behaviour.
5. The live workflow must remain manual-only.
6. A clean reset/fresh-install acceptance should converge on the same history and rolling state as ordinary operation.
