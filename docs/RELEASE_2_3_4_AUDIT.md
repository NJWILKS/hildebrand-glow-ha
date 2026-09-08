# 2.3.4 regression audit

## Why this release exists

2.3.3 can produce a large negative current-day Energy-dashboard adjustment after an imported-history reset/reload. A real acceptance run on 2026-09-08 showed approximately -7.67 MWh in one hour while the household's real current-day use was small.

2.3.4 is therefore a regression-audit release, not another narrow patch release.

## End-game acceptance criterion

The user-facing outcome is a daily stacked cost chart with two trustworthy components:

- **Usage cost** — consumption priced at the unit rate effective for that date/interval.
- **Standing charge** — the standing charge effective for that UK-local billing day, applied exactly once.

For a flat tariff:

`usage cost = Σ(PT30M kWh × effective unit rate)`

`daily total = usage cost + effective standing charge`

The cumulative `sum` fields Home Assistant needs are implementation detail. The meaningful values are the dated interval/day quantities on the graph.

## Source-of-truth model

- Glow PT30M consumption rows are interval energy, not a lifetime meter register.
- Glow `tariff-list` is the effective-dated tariff and standing-charge ledger.
- For a **flat tariff**, PT30M consumption × the explicit tariff rate is authoritative for the usage-cost component.
- For **TOU/dynamic tariffs**, where one flat rate cannot describe the day, Glow PT30M cost is authoritative for the usage-cost shape.
- The effective tariff standing charge is applied once per UK-local billing day whenever it is available.
- Glow P1D cost is a reconciliation oracle for completed days. It must remain close to the independently constructed usage + standing components, but must not distort those components merely to make a rounded aggregate match exactly.
- Home Assistant Recorder statistics are a separate persisted model and must never infer a fake meter reset from our ingestion lifecycle.

## Required invariants

### Consumption

1. Every non-null PT30M interval is counted exactly once.
2. A real zero interval is retained as data.
3. Completed-day history and current-day ingestion have one statistics owner; there must be no mixed Recorder/import ownership at the same statistic ID.
4. Repeating an identical API response is idempotent.
5. Restart/reload/reset cannot create a negative hourly Energy delta.
6. A shorter or missing current-day response cannot move the external Energy sum backwards.
7. A historical Bright revision can change the rebuilt historical series without creating a synthetic current-day meter-reset adjustment.
8. UK-local 46/48/50 interval days remain correct.
9. The visible consumption sensor is contextual to today; the lifetime cumulative sum is not exposed as the useful user-facing value.

### Tariff and cost

1. Tariff history is fetched before historical cost reconciliation.
2. Historical tariff changes are effective-dated.
3. A flat tariff must expose one exact unit rate and one exact standing charge for its effective period.
4. Flat-tariff usage cost is derived from consumption × effective unit rate, not inferred from a daily residual.
5. TOU/dynamic usage retains Glow PT30M cost because a single rate cannot price it correctly.
6. Standing charge is applied exactly once per local billing day, including the open day when the effective tariff is already known.
7. Usage cost and standing charge are imported as separate, stackable daily statistics with one integration-owned writer.
8. Usage + standing must reconcile closely with Glow P1D on completed days; material disagreement is logged and fails the protected live contract.
9. The dedicated external total-cost statistic equals the same usage + standing composition used by the stacked chart.
10. Reconciliation is idempotent and cannot double-import cost.
11. Settings default to the latest persisted Glow tariff values unless the user has explicitly overridden them.

### Lifecycle

1. Exactly one consumption-history worker/task exists per config entry.
2. Exactly one cost-ingestion worker/task exists per config entry.
3. Config-entry unload cancels integration-owned tasks without returning a non-awaitable job to Home Assistant.
4. Reset clears only integration-owned imported statistics/state and leaves a deterministic rebuild path.

### Release

1. `manifest.json` version matches the release tag.
2. Normal CI: pytest + Ruff + Hassfest + HACS all green.
3. Protected live contract passes against Bright/Glowmarkt, including tariff unit-rate and standing-charge reconciliation.
4. A real Home Assistant reset/rebuild acceptance run shows realistic positive consumption history.
5. The real Home Assistant stacked electricity cost card shows credible **Usage Cost + Standing Charge** daily bars and the two components add to the daily total.

## Audit findings at branch creation

- `__init__.py` and `sensor.py` both started `async_cost_ingestion_worker`, so cost task ownership was duplicated.
- `manifest.json` still reported 2.3.1 even though repository releases reached 2.3.3.
- Existing consumption regression tests mocked `async_import_statistics`; they validated helper arithmetic but did not prove the resulting Home Assistant Recorder/Energy continuity through clear/reload/backfill.
- The architecture mixed imported completed-day statistics with Recorder-owned current-day statistics on the same `sensor.*` statistic ID. Clearing/rebuilding that statistic mid-day could detach Recorder's current-hour baseline from the imported historical sum, consistent with the observed large negative adjustment.
- Historical cost composition treated P1D as something to force the component split to match. That made standing-charge inference useful as a fallback, but it was the wrong primary model once `tariff-list` had been proven to contain effective unit rates and standing charges.

## 2.3.4 release gate

This draft stays unreleased until the negative-energy scenario, tariff pricing, standing-charge application, lifecycle ownership and stacked-cost output are all represented by regression coverage and pass using the final statistics ownership model.