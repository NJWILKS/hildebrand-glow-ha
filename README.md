# Hildebrand Glow (Bright App) for Home Assistant

[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A maintained rescue fork of the Home Assistant integration for UK SMETS2 smart meters using the Hildebrand Glow / Bright API.

## What this fork adds

- **Full historical electricity and gas consumption import** into Home Assistant Recorder statistics using the original half-hour timestamps.
- **Full-history discovery without an arbitrary look-back limit**: Glowmarkt `first-time` is used as a cheap locator, then the PT30M readings endpoint determines the first actual billing interval.
- **DST-safe PT30M history retrieval** in bounded chunks suitable for the Glowmarkt API.
- **Multi-site Bright account support** with explicit meter-site selection.
- **Stable entity identity** that prefers the real Glow resource ID, so re-adding a site does not create a new logical meter unnecessarily.
- **API resilience**: one paced request lane, `Retry-After` support, bounded retries for HTTP 429, transient 5xx responses and transient connection drops. API failures are never interpreted as an empty history boundary.
- **Lower API pressure**: consumption defaults to 15-minute polling; API-derived cost resources default to 60 minutes; both are configurable with a 5-minute minimum.
- **Non-blocking history backfill** so Home Assistant setup is not held open while historical data is retrieved.
- **Home Assistant validation and regression CI** with pytest, Ruff, Hassfest and HACS validation.

## Installation

### HACS custom repository

1. Open **HACS** in Home Assistant.
2. Open the three-dot menu and choose **Custom repositories**.
3. Add `https://github.com/NJWILKS/hildebrand-glow-ha` as an **Integration**.
4. Search for **Hildebrand Glow (Bright App)** and download it.
5. Restart Home Assistant.
6. Go to **Settings → Devices & Services → Add Integration** and search for **Hildebrand Glow**.

For a clean acceptance test, install this fork only; do not keep a second copy of another integration under the same `hildebrand_glow` domain.

## Configuration

The setup flow asks for:

- Bright email address and password
- Bright meter site / location
- electricity unit rate and standing charge
- gas unit rate and standing charge

After setup, **Configure** also exposes:

- consumption refresh interval, default **15 minutes**
- API-cost refresh interval, default **60 minutes**

Intervals below five minutes are blocked to reduce the risk of Glowmarkt HTTP 429 responses.

## Energy history model

The consumption sensors are cumulative `TOTAL_INCREASING` energy sensors for Home Assistant. Historical Bright data is imported directly into Recorder statistics using the original half-hour readings, aggregated into Home Assistant's hourly statistics while preserving the real shape of the day.

On a fresh install the integration:

1. asks Glowmarkt `first-time` for an approximate start locator;
2. queries a small PT30M window around that locator and treats the earliest non-null reading as the real start of available billing history;
3. retrieves all complete history from that resolved boundary to today in DST-safe PT30M chunks;
4. imports the hourly statistics in the background;
5. stores cumulative day state so restarts do not add the same completed day twice.

A genuine `0.0` reading counts as data. Missing/null readings do not. The current incomplete UK-local day is not treated as completed historical data.

`first-time` is deliberately **not** treated as authoritative consumption data. Live testing showed that Glowmarkt metadata can continue to point at historical intervals which the readings endpoint no longer returns. The PT30M readings endpoint therefore decides which intervals actually exist for billing/history purposes.

If Glowmarkt temporarily fails, disconnects or rate-limits a request, the integration retries with bounded backoff. A failed request is never converted into a false end-of-history marker.

For implementation detail, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Sensors

The integration creates electricity and gas consumption sensors, API cost sensors when those resources exist, calculated daily electricity/gas/combined cost sensors, and standing-charge totals.

Glow/Bright DCC data is not real-time and can arrive a day or more late. The coordinator therefore looks for the latest completed day containing actual readings rather than assuming yesterday is already complete.

## Real-data regression contract

The test suite includes a privacy-safe oracle derived from a contributed real Bright electricity export containing **19,346 half-hour readings** across 404 UK-local days. The raw household CSV is not stored in the repository.

The protected live test uses repository environment credentials and verifies:

- Glowmarkt `first-time` still locates the known historical neighbourhood;
- the PT30M readings endpoint resolves an actual first available interval at or after that locator;
- `last-time` has not regressed behind the known export;
- stable historical PT30M slices still match known data across both UK DST transitions and a recent completed day.

The first exported day is **not** treated as immutable because live testing proved the earliest historic rows can be revised or disappear while `first-time` metadata remains unchanged.

The live job compares counts, totals and SHA-256 fingerprints without printing raw household readings or credentials to Actions logs. It is **manual-only** and runs separately from normal pull-request CI.

See [docs/TESTING.md](docs/TESTING.md) for the test strategy and live-contract rules.

## Development

Normal pull-request CI never uses Bright credentials. It runs mocked/unit tests, Ruff, Hassfest and HACS validation. The protected live contract is separate and uses the `glow-live` GitHub Environment.

Bug fixes should arrive with a regression test. Historical/cumulative energy changes require particular care around duplicate imports, restart behaviour, missing readings, UK-local day boundaries, DST and API revisions.

Repository/agent maintenance rules are documented in [AGENTS.md](AGENTS.md).

## Acknowledgements

This fork builds on the work of the original `xmcdanx` / `McDon22` Hildebrand Glow integration and its contributors. Operational patterns were also reviewed against `jonandel/ha-hildebrandglow-dcc`, including resource-based identity, separated polling concerns and API-protective polling intervals.

The project remains MIT licensed; see [LICENSE](LICENSE).
