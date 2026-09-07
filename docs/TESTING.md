# Testing strategy

The project uses two deliberately separate test layers: hermetic pull-request CI and a protected live Glowmarkt contract.

## Normal CI

Normal CI must never call the live Glowmarkt API and must never require Bright credentials.

Every pull request runs:

- pytest with `pytest-homeassistant-custom-component`;
- Ruff;
- Hassfest;
- HACS validation.

The standard test suite covers API parsing, config flow, multi-site selection, sensor metadata, identity, coordinator behaviour, cumulative/restart safety, DST handling, history chunking, cost aggregation semantics, tariff ordering, backoff and failure behaviour.

Run locally with:

```bash
python -m pip install -r requirements-dev.txt
pytest
ruff check .
```

## Regression-test rule

A behavioural bug fix should include a regression test that describes the failure independently of the implementation.

For historical energy and cost changes, tests should explicitly consider:

- missing/null readings;
- genuine zero readings;
- delayed DCC data;
- repeated polling;
- Home Assistant restart/reload;
- out-of-order older data;
- UK-local midnight boundaries;
- spring DST (46 PT30M intervals);
- autumn DST (50 PT30M intervals);
- query end-boundary buckets;
- PT30M cost being usage-only;
- P1D cost containing standing charge;
- completed-day residual `P1D - PT30M`;
- P1D not yet available (`daily_pending`);
- negative/contradictory residuals returning `unknown` rather than a negative charge;
- effective-dated tariff ordering;
- 429/5xx responses;
- transient connection drops;
- interruption during background backfill.

## Cost-component statistics

Historical cost-component tests verify that:

- usage cost and standing charge are imported as separate GBP statistics;
- the daily `state` is the amount for that component on that UK-local day;
- the `sum` is cumulative for Recorder compatibility;
- DST-local midnights are converted to the correct UTC statistics timestamp;
- an unknown standing-charge split does not invent a historical value.

These statistics are designed for Home Assistant's native Statistics Graph card with `chart_type: bar-stack` and one `state` series per component.

## Tariff ledger

`tariff-list` is treated as an effective-dated ledger. Tests cover stable ordering by `effectiveDate` / `from` and ensure tariff records remain separate from API-derived billing costs.

The raw tariff values are not used to recalculate historical cost in tests. The cost resource remains authoritative.

## Real-data oracle

A privacy-safe oracle is derived from a contributed real Bright electricity export.

The repository stores summary/fingerprint data only, not the raw household CSV. The oracle includes useful structural facts such as row counts, date boundaries, totals and SHA-256 fingerprints for selected historic slices.

Those totals/fingerprints describe the export **at the time it was taken**. They are useful diagnostic evidence, but they are not assumed to be an immutable billing ledger: Glowmarkt can revise historic values later.

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
- cost aggregation periods;
- standing-charge reconciliation;
- tariff-list retrieval;
- retry/backoff transport;
- DST/day-boundary handling;
- historical backfill/import semantics.

Do not trigger it on every branch push. Normal CI is the default gate; the live contract is an explicit pre-release/acceptance gate.

## Release acceptance

Before merging a release-affecting PR:

1. Tests, Ruff, Hassfest and HACS must be green.
2. Relevant regression tests must exist.
3. If the API/history path changed, the protected live contract must pass.
4. Documentation must match the shipped behaviour.
5. The live workflow must remain manual-only.
