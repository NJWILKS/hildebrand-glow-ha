# Repository maintenance rules

This repository is a maintained rescue fork of a Home Assistant custom integration. Changes should favour correctness, testability and conservative API behaviour over speculative rewrites.

## Core rules

- Never commit Bright credentials, auth tokens, virtual-entity IDs, resource IDs tied to a household, addresses, account data or raw household usage exports.
- Never call the live Glowmarkt API from ordinary pull-request CI.
- Bug fixes require regression tests.
- Historical/cumulative-energy changes require tests for restart safety and duplicate prevention.
- Preserve Home Assistant async/non-blocking patterns.
- Keep pull requests focused and reviewable.
- Do not change `main` directly for behavioural work; use a branch and PR.
- Run pytest/Ruff/Hassfest/HACS before merge.
- Run the protected live contract manually when API/history semantics change.
- Keep `.github/workflows/live-contract.yml` manual-only unless a temporary diagnostic trigger is explicitly required, and remove that trigger before merge.

## History/source-of-truth rule

`first-time` metadata is a locator, not authoritative consumption data.

The PT30M readings endpoint is authoritative for which billing intervals actually exist. A real `0.0` reading is data. A null/missing reading is not data. An API error is neither and must propagate/retry rather than being treated as empty history.

## Review roles

### Test/reproduction review

Independently describe the bug or requirement as a regression test before relying on the proposed implementation. Prefer tests that would fail against the previous behaviour for the intended reason.

### Home Assistant review

Check:

- config-entry lifecycle;
- entity/unique-ID stability;
- state classes and device classes;
- Recorder/statistics semantics;
- async/non-blocking behaviour;
- reload/unload behaviour;
- Home Assistant version compatibility.

### Adversarial data review

Actively test:

- multiple Bright sites;
- electricity-only or gas-only accounts;
- delayed/missing data;
- real zero readings;
- 429/5xx/disconnects;
- spring/autumn DST;
- midnight query boundaries;
- restart during/after backfill;
- repeated/out-of-order completed days;
- revised historic API data.

### Maintenance review

Watch for drift in:

- Home Assistant APIs;
- HACS validation;
- Hassfest;
- Python/test-harness versions;
- Glowmarkt API behaviour/documentation.

Maintenance changes should be proposed through normal CI rather than silently altering production behaviour.

## Live test safety

The protected live test may use `GLOWMARKT_USERNAME` and `GLOWMARKT_PASSWORD` from the `glow-live` GitHub Environment only.

Do not log credentials, tokens or raw household consumption. Keep diagnostics bounded and privacy-safe.

## Merge standard

A change is ready when:

- the intended behaviour is covered by tests;
- normal CI is green;
- live API/history changes have passed the protected live contract;
- docs describe the final behaviour;
- there are no unexplained test skips or swallowed API failures.
