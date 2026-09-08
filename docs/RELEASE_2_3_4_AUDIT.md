# 2.3.4 regression audit

## Why this release exists

2.3.3 can produce a large negative current-day Energy-dashboard adjustment after an imported-history reset/reload. A real acceptance run on 2026-09-08 showed approximately -7.67 MWh in one hour while the household's real current-day use was small.

2.3.4 is therefore a regression-audit release, not another narrow patch release.

## Source-of-truth model

- Glow PT30M consumption rows are interval energy, not a lifetime meter register.
- Glow P1D cost is authoritative for a completed day's total cost.
- Glow PT30M cost is usage-only intraday cost.
- Glow `tariff-list` is the effective-dated tariff/standing-charge ledger.
- Home Assistant Recorder statistics are a separate persisted model and must never infer a fake meter reset from our ingestion lifecycle.

## Required invariants

### Consumption

1. Every non-null PT30M interval is counted exactly once.
2. A real zero interval is retained as data.
3. Completed-day history and current-day ingestion have one statistics owner; there must be no mixed Recorder/import ownership at the same statistic ID.
4. Repeating an identical API response is idempotent.
5. Restart/reload/reset cannot create a negative hourly Energy delta.
6. A shorter or missing current-day response cannot move cumulative consumption backwards.
7. A historical Bright revision can change the rebuilt historical series without creating a synthetic current-day meter-reset adjustment.
8. UK-local 46/48/50 interval days remain correct.

### Cost

1. Tariff history is fetched before historical cost reconciliation.
2. P1D remains authoritative for completed-day total cost.
3. PT30M supplies intraday usage-cost shape.
4. Standing charge is applied exactly once per completed local day.
5. Historical tariff changes are effective-dated; the current configured tariff is calibration/fallback only.
6. Reconciliation is idempotent and cannot double-import cost.

### Lifecycle

1. Exactly one consumption-history worker/task exists per config entry.
2. Exactly one cost-ingestion worker/task exists per config entry.
3. Config-entry unload cancels integration-owned tasks without returning a non-awaitable job to Home Assistant.
4. Reset clears only integration-owned imported statistics/state and leaves a deterministic rebuild path.

### Release

1. `manifest.json` version matches the release tag.
2. Normal CI: pytest + Ruff + Hassfest + HACS all green.
3. Protected live contract passes against Bright/Glowmarkt.
4. A real Home Assistant reset/rebuild acceptance run shows realistic positive consumption and cost history before the release is published.

## Audit findings at branch creation

- `__init__.py` and `sensor.py` both start `async_cost_ingestion_worker`, so cost task ownership is duplicated.
- `manifest.json` still reports 2.3.1 even though repository releases reached 2.3.3.
- Existing consumption regression tests mock `async_import_statistics`; they validate helper arithmetic but do not prove the resulting Home Assistant Recorder/Energy `sum` continuity through clear/reload/backfill.
- The current architecture mixes imported completed-day statistics with Recorder-owned current-day statistics on the same `sensor.*` statistic ID. Clearing/rebuilding that statistic mid-day can detach Recorder's current-hour `sum` baseline from the imported historical cumulative sum, which is consistent with the observed large negative adjustment.

## 2.3.4 release gate

This draft stays unreleased until the observed negative-energy scenario is represented by regression coverage and passes using the final statistics ownership model.