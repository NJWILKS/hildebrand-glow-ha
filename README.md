# Hildebrand Glow (Bright App) for Home Assistant

[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A maintained rescue fork of the Home Assistant integration for UK SMETS2 smart meters using the Hildebrand Glow / Bright API.

## What this fork adds

- **Full historical electricity and gas consumption import** into Home Assistant Recorder statistics using the original half-hour timestamps.
- **Authoritative history discovery** using Glowmarkt `first-time` rather than an arbitrary look-back limit.
- **DST-safe PT30M history retrieval** in bounded chunks suitable for the Glowmarkt API.
- **Multi-site Bright account support** with explicit meter-site selection.
- **Stable entity identity** that prefers the real Glow resource ID, so re-adding a site does not create a new logical meter unnecessarily.
- **API resilience**: one paced request lane, `Retry-After` support, bounded retries for HTTP 429 and transient 5xx failures, and real API failures are never interpreted as an empty history boundary.
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

For a clean acceptance test, install this fork only; do not keep a second copy of the abandoned integration under the same `hildebrand_glow` domain.

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

1. asks Glowmarkt for the resource's authoritative first available timestamp;
2. retrieves all complete history from that boundary to today in DST-safe PT30M chunks;
3. imports the hourly statistics in the background;
4. stores the cumulative day boundary so restarts do not add the same completed day twice.

The current incomplete UK-local day is not treated as completed historical data. If Glowmarkt temporarily fails or rate-limits a request, the backfill fails safely and can retry rather than recording a false end-of-history marker.

## Sensors

The integration creates electricity and gas consumption sensors, API cost sensors when those resources exist, calculated daily electricity/gas/combined cost sensors, and standing-charge totals.

Glow/Bright DCC data is not real-time and can arrive a day or more late. The coordinator therefore looks for the latest completed day containing actual readings rather than assuming yesterday is already complete.

## Real-data regression contract

The test suite includes a privacy-safe oracle derived from a contributed real Bright electricity export containing **19,346 consecutive half-hour readings** across 404 UK-local days. The raw household CSV is not stored in the repository.

The protected live test uses repository environment credentials and verifies:

- Glowmarkt `first-time` agrees with the known historical boundary;
- `last-time` has not regressed behind the known export;
- immutable historical PT30M data still matches the known first day, both UK DST transitions, and the last complete exported day.

The live job compares counts, totals and SHA-256 fingerprints without printing the raw household readings or credentials to Actions logs.

## Development

Normal pull-request CI never uses Bright credentials. It runs mocked/unit tests, Ruff, Hassfest and HACS validation. The protected live contract is separate and uses the `glow-live` GitHub Environment.

Bug fixes should arrive with a regression test. Historical/cumulative energy changes require particular care around duplicate imports, restart behaviour, missing readings, UK-local day boundaries and DST.

## Acknowledgements

This fork builds on the work of the original `xmcdanx` / `McDon22` Hildebrand Glow integration and its contributors. Operational patterns were also reviewed against `jonandel/ha-hildebrandglow-dcc`, including resource-based identity, separated polling concerns and API-protective polling intervals.

The project remains MIT licensed; see [LICENSE](LICENSE).
