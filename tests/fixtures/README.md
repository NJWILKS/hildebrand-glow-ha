# Electricity regression fixture

`electricity_consumption.csv` is a real Glowmarkt/Bright electricity export contributed
for regression testing.

The fixture intentionally preserves the original UTC timestamps and kWh values so tests
can validate UK-local day boundaries, daylight-saving transitions, daily totals, hourly
aggregation and historical backfill behaviour.

It contains no account ID, username, address, meter serial number or location name.

Coverage:

- 19,346 consecutive 30-minute readings
- UK-local start: 2025-07-30 00:00 BST
- UK-local end: 2026-09-06 00:30 BST
- Includes the 2025 autumn and 2026 spring UK DST transitions
