"""Resumable PT30M usage, cost and tariff history ledger."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .api import HISTORY_INTERVAL_DAYS, UK_TZ, GlowmarktApiClient, GlowmarktApiError
from .const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_ELECTRICITY_COST,
    CLASSIFIER_GAS_CONSUMPTION,
    CLASSIFIER_GAS_COST,
    DOMAIN,
    GLOWMARKT_API_BASE,
)
from .tariff import parse_tariff_periods, tariff_period_as_dict

_LOGGER = logging.getLogger(__name__)

INTERVAL_HISTORY_STORAGE_VERSION = 1
INTERVAL_HISTORY_SCHEMA_VERSION = 1

COMMODITY_RESOURCES: dict[str, tuple[str, str]] = {
    "electricity": (
        CLASSIFIER_ELECTRICITY_CONSUMPTION,
        CLASSIFIER_ELECTRICITY_COST,
    ),
    "gas": (
        CLASSIFIER_GAS_CONSUMPTION,
        CLASSIFIER_GAS_COST,
    ),
}


def _floor_half_hour(value: datetime) -> datetime:
    """Return the exclusive UTC boundary for the latest completed PT30M slot."""
    value = value.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return value.replace(minute=30 if value.minute >= 30 else 0)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _month_key(value: datetime) -> str:
    local = value.astimezone(UK_TZ)
    return f"{local.year:04d}-{local.month:02d}"


def _tariff_reference(
    periods: list[dict[str, Any]],
    timestamp: datetime,
) -> str | None:
    """Return the effective-period key applicable to this interval."""
    day = timestamp.astimezone(UK_TZ).date()
    for period in periods:
        effective_from = datetime.fromisoformat(period["effective_from"]).date()
        effective_to_raw = period.get("effective_to")
        effective_to = (
            datetime.fromisoformat(effective_to_raw).date()
            if effective_to_raw
            else None
        )
        if day >= effective_from and (effective_to is None or day < effective_to):
            return period["effective_from"]
    return None


class IntervalHistoryStore:
    """Bounded Home Assistant stores for history metadata and monthly intervals."""

    def __init__(self, hass: HomeAssistant, site_id: str) -> None:
        self.hass = hass
        self.site_id = site_id
        self._metadata = Store(
            hass,
            INTERVAL_HISTORY_STORAGE_VERSION,
            f"{DOMAIN}_{site_id}_interval_history",
        )

    def _month_store(self, commodity: str, month: str) -> Store:
        safe_month = month.replace("-", "_")
        return Store(
            self.hass,
            INTERVAL_HISTORY_STORAGE_VERSION,
            f"{DOMAIN}_{self.site_id}_interval_history_{commodity}_{safe_month}",
        )

    async def async_load_metadata(self) -> dict[str, Any]:
        state = await self._metadata.async_load() or {}
        if int(state.get("schema_version", 0) or 0) != INTERVAL_HISTORY_SCHEMA_VERSION:
            return {
                "schema_version": INTERVAL_HISTORY_SCHEMA_VERSION,
                "commodities": {},
            }
        state.setdefault("commodities", {})
        return state

    async def async_save_metadata(self, state: dict[str, Any]) -> None:
        state["schema_version"] = INTERVAL_HISTORY_SCHEMA_VERSION
        await self._metadata.async_save(state)

    async def async_load_month(
        self,
        commodity: str,
        month: str,
    ) -> dict[str, dict[str, Any]]:
        state = await self._month_store(commodity, month).async_load() or {}
        intervals = state.get("intervals", {})
        return intervals if isinstance(intervals, dict) else {}

    async def async_upsert_intervals(
        self,
        commodity: str,
        records: list[dict[str, Any]],
    ) -> None:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            grouped[_month_key(_parse_utc(record["timestamp"]))].append(record)

        for month, month_records in grouped.items():
            store = self._month_store(commodity, month)
            state = await store.async_load() or {}
            intervals = state.get("intervals", {})
            if not isinstance(intervals, dict):
                intervals = {}
            for record in month_records:
                intervals[record["timestamp"]] = record
            await store.async_save(
                {
                    "schema_version": INTERVAL_HISTORY_SCHEMA_VERSION,
                    "commodity": commodity,
                    "month": month,
                    "intervals": intervals,
                }
            )


async def _first_interval(
    api_client: GlowmarktApiClient,
    resources: dict[str, dict[str, Any]],
    usage_classifier: str,
    cost_classifier: str,
) -> datetime | None:
    """Return the earliest real PT30M interval exposed by either resource."""
    first_times: list[datetime] = []
    for classifier in (usage_classifier, cost_classifier):
        resource = resources.get(classifier)
        if not resource:
            continue
        first = await api_client.get_first_available_reading_time(resource["resource_id"])
        if first is not None:
            first_times.append(first.astimezone(timezone.utc))
    return min(first_times) if first_times else None


async def _load_tariffs(
    api_client: GlowmarktApiClient,
    resources: dict[str, dict[str, Any]],
    cost_classifier: str,
) -> dict[str, Any]:
    resource = resources.get(cost_classifier)
    if not resource:
        return {"raw": [], "periods": []}

    data = await api_client._get_json(  # noqa: SLF001
        f"{GLOWMARKT_API_BASE}/resource/{resource['resource_id']}/tariff-list"
    )
    if not isinstance(data, dict) or data.get("status") not in (None, "OK"):
        raise GlowmarktApiError("Glowmarkt tariff-list query returned an error")
    raw = data.get("data") or []
    if not isinstance(raw, list):
        raw = []
    raw = [item for item in raw if isinstance(item, dict)]
    periods = [
        tariff_period_as_dict(period)
        for period in parse_tariff_periods(raw)
    ]
    return {"raw": raw, "periods": periods}


async def _pt30m_values(
    api_client: GlowmarktApiClient,
    resource_id: str,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, float]]:
    rows = await api_client._request_readings(  # noqa: SLF001
        resource_id,
        start,
        end,
        "PT30M",
    )
    values: list[tuple[datetime, float]] = []
    for row in rows:
        if len(row) <= 1 or row[1] is None:
            continue
        timestamp = datetime.fromtimestamp(float(row[0]), tz=timezone.utc)
        if start <= timestamp < end:
            values.append((timestamp, float(row[1])))
    values.sort(key=lambda item: item[0])
    return values


async def _fetch_records(
    api_client: GlowmarktApiClient,
    resources: dict[str, dict[str, Any]],
    usage_classifier: str,
    cost_classifier: str,
    tariffs: dict[str, Any],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    usage_resource = resources.get(usage_classifier)
    cost_resource = resources.get(cost_classifier)

    usage = (
        await _pt30m_values(api_client, usage_resource["resource_id"], start, end)
        if usage_resource
        else []
    )
    cost = (
        await _pt30m_values(api_client, cost_resource["resource_id"], start, end)
        if cost_resource
        else []
    )

    usage_by_timestamp = {_utc_iso(timestamp): value for timestamp, value in usage}
    cost_by_timestamp = {_utc_iso(timestamp): value for timestamp, value in cost}
    timestamps = sorted(set(usage_by_timestamp) | set(cost_by_timestamp))

    records: list[dict[str, Any]] = []
    periods = tariffs.get("periods", [])
    for timestamp_text in timestamps:
        timestamp = _parse_utc(timestamp_text)
        records.append(
            {
                "timestamp": timestamp_text,
                "usage_kwh": usage_by_timestamp.get(timestamp_text),
                "cost_pence": cost_by_timestamp.get(timestamp_text),
                "tariff_effective_from": _tariff_reference(periods, timestamp),
            }
        )
    return records


async def async_populate_interval_history(
    hass: HomeAssistant,
    api_client: GlowmarktApiClient,
    resources: dict[str, dict[str, Any]],
    site_id: str,
    *,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Populate or resume the local PT30M ledger to the latest completed interval."""
    target_end = _floor_half_hour(now_utc or datetime.now(timezone.utc))
    repository = IntervalHistoryStore(hass, site_id)
    metadata = await repository.async_load_metadata()
    commodities = metadata.setdefault("commodities", {})

    for commodity, (usage_classifier, cost_classifier) in COMMODITY_RESOURCES.items():
        if usage_classifier not in resources and cost_classifier not in resources:
            continue

        state = commodities.setdefault(commodity, {})
        tariffs = await _load_tariffs(api_client, resources, cost_classifier)
        state["tariffs"] = tariffs

        first = (
            _parse_utc(state["first_interval"])
            if state.get("first_interval")
            else await _first_interval(
                api_client,
                resources,
                usage_classifier,
                cost_classifier,
            )
        )
        if first is None:
            state["status"] = "no_data"
            await repository.async_save_metadata(metadata)
            continue

        state["first_interval"] = _utc_iso(first)
        cursor = _parse_utc(state["cursor_utc"]) if state.get("cursor_utc") else first
        cursor = max(cursor, first)

        while cursor < target_end:
            chunk_end = min(
                cursor + timedelta(days=HISTORY_INTERVAL_DAYS),
                target_end,
            )
            records = await _fetch_records(
                api_client,
                resources,
                usage_classifier,
                cost_classifier,
                tariffs,
                cursor,
                chunk_end,
            )

            # Persist data before advancing the cursor. If Home Assistant stops
            # between these writes the same chunk is replayed and timestamp-keyed
            # upserts make that replay idempotent.
            await repository.async_upsert_intervals(commodity, records)
            state["cursor_utc"] = _utc_iso(chunk_end)
            if records:
                state["last_interval"] = records[-1]["timestamp"]
            state["status"] = "populating" if chunk_end < target_end else "current"
            await repository.async_save_metadata(metadata)
            cursor = chunk_end

        if cursor >= target_end:
            state["status"] = "current"
            state["cursor_utc"] = _utc_iso(target_end)
            await repository.async_save_metadata(metadata)

        _LOGGER.info(
            "PT30M interval history %s is %s through %s",
            commodity,
            state.get("status"),
            state.get("cursor_utc"),
        )

    return metadata


async def async_interval_history_worker(
    hass: HomeAssistant,
    api_client: GlowmarktApiClient,
    resources: dict[str, dict[str, Any]],
    site_id: str,
) -> None:
    """Run one transparent, resumable history population pass."""
    await async_populate_interval_history(
        hass,
        api_client,
        resources,
        site_id,
    )
