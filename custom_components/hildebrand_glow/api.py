"""Glowmarkt API client for Hildebrand Glow integration."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import ClientError, ClientResponseError

from .const import GLOWMARKT_API_BASE, GLOWMARKT_APP_ID

_LOGGER = logging.getLogger(__name__)
UK_TZ = ZoneInfo("Europe/London")

# Glowmarkt returns Unix timestamps. Scanning to the Unix epoch gives us a
# deterministic lower boundary without imposing an arbitrary history horizon.
UNIX_EPOCH_YEAR = 1970

# Hildebrand documents a maximum PT30M query span of 10 days. Use nine UK-local
# calendar days so a 25-hour autumn DST day can never make the UTC span exceed
# that API limit.
HISTORY_INTERVAL_DAYS = 9
HISTORY_MAX_RETRIES = 4


@dataclass
class DailyReading:
    """One complete UK-local day's reading and its real 30-minute intervals."""

    day: str
    value: float
    intervals: list[tuple[datetime, float]]


class GlowmarktAuthError(Exception):
    """Exception for authentication errors."""


class GlowmarktApiError(Exception):
    """Exception for API errors."""


class GlowmarktApiClient:
    """Async client for the Glowmarkt API."""

    def __init__(
        self,
        username: str,
        password: str,
        session: aiohttp.ClientSession,
    ) -> None:
        self._username = username
        self._password = password
        self._session = session
        self._token: str | None = None
        self._token_expiry: datetime | None = None
        self._virtual_entity_id: str | None = None
        self._resources: dict[str, dict[str, Any]] = {}

    async def authenticate(self) -> bool:
        headers = {
            "Content-Type": "application/json",
            "applicationId": GLOWMARKT_APP_ID,
        }
        payload = {"username": self._username, "password": self._password}
        try:
            async with self._session.post(
                f"{GLOWMARKT_API_BASE}/auth",
                headers=headers,
                json=payload,
            ) as response:
                if response.status == 401:
                    raise GlowmarktAuthError("Invalid username or password")
                response.raise_for_status()
                data = await response.json()
                if data.get("valid"):
                    self._token = data["token"]
                    self._token_expiry = datetime.now() + timedelta(days=6)
                    return True
                raise GlowmarktAuthError("Authentication failed: invalid response")
        except ClientResponseError as err:
            raise GlowmarktAuthError(f"Authentication failed: {err}") from err
        except ClientError as err:
            raise GlowmarktApiError(f"Connection error: {err}") from err

    async def _ensure_authenticated(self) -> None:
        if (
            self._token is None
            or self._token_expiry is None
            or datetime.now() > self._token_expiry
        ):
            await self.authenticate()

    def _get_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "applicationId": GLOWMARKT_APP_ID,
            "token": self._token or "",
        }

    async def get_virtual_entities(self) -> list[dict[str, Any]]:
        await self._ensure_authenticated()
        try:
            async with self._session.get(
                f"{GLOWMARKT_API_BASE}/virtualentity",
                headers=self._get_headers(),
            ) as response:
                response.raise_for_status()
                data = await response.json()
                return data if isinstance(data, list) else []
        except ClientError as err:
            raise GlowmarktApiError(
                f"Failed to get virtual entities: {err}"
            ) from err

    async def discover_resources(
        self,
        virtual_entity_id: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Discover resources for one selected virtual entity, or all entities."""
        await self._ensure_authenticated()
        if virtual_entity_id:
            self._virtual_entity_id = virtual_entity_id
            ve_ids = [virtual_entity_id]
        else:
            virtual_entities = await self.get_virtual_entities()
            if not virtual_entities:
                return {}
            ve_ids = [ve.get("veId") for ve in virtual_entities if ve.get("veId")]

        self._resources = {}
        for ve_id in ve_ids:
            try:
                async with self._session.get(
                    f"{GLOWMARKT_API_BASE}/virtualentity/{ve_id}/resources",
                    headers=self._get_headers(),
                ) as response:
                    response.raise_for_status()
                    data = await response.json()
                    for resource in data.get("resources", []):
                        resource_id = resource.get("resourceId")
                        classifier = resource.get("classifier")
                        if resource_id and classifier:
                            self._resources[classifier] = {
                                "resource_id": resource_id,
                                "name": resource.get("name", classifier),
                                "classifier": classifier,
                                "base_unit": resource.get("baseUnit", ""),
                            }
            except ClientError as err:
                _LOGGER.error("Failed to get resources for %s: %s", ve_id, err)
        return self._resources

    async def _request_readings(
        self,
        resource_id: str,
        start: datetime,
        end: datetime,
        period: str,
    ) -> list[list[Any]]:
        """Fetch a readings window, retrying 429s but never treating errors as no data."""
        params = {
            "from": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "to": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "period": period,
            "offset": 0,
            "function": "sum",
        }

        for attempt in range(HISTORY_MAX_RETRIES + 1):
            try:
                async with self._session.get(
                    f"{GLOWMARKT_API_BASE}/resource/{resource_id}/readings",
                    headers=self._get_headers(),
                    params=params,
                ) as response:
                    if response.status == 429 and attempt < HISTORY_MAX_RETRIES:
                        headers = getattr(response, "headers", {})
                        retry_after = headers.get("Retry-After") if headers else None
                        try:
                            delay = float(retry_after) if retry_after is not None else 2**attempt
                        except (TypeError, ValueError):
                            delay = 2**attempt
                        _LOGGER.warning(
                            "Glowmarkt rate limited %s; retrying in %.1fs",
                            resource_id,
                            delay,
                        )
                        await asyncio.sleep(min(delay, 30.0))
                        continue

                    response.raise_for_status()
                    data = await response.json()
                    if data.get("status") != "OK":
                        raise GlowmarktApiError(
                            f"Readings query failed for {resource_id}: {data.get('status')}"
                        )
                    rows = data.get("data", [])
                    return rows if isinstance(rows, list) else []
            except ClientResponseError as err:
                if err.status == 429 and attempt < HISTORY_MAX_RETRIES:
                    await asyncio.sleep(min(float(2**attempt), 30.0))
                    continue
                raise GlowmarktApiError(
                    f"Readings query failed for {resource_id}: {err}"
                ) from err
            except ClientError as err:
                raise GlowmarktApiError(
                    f"Readings query failed for {resource_id}: {err}"
                ) from err

        raise GlowmarktApiError(f"Readings query exhausted retries for {resource_id}")

    async def _fetch_day_reading(
        self,
        resource_id: str,
        day_start_uk: datetime,
        day_end_uk: datetime,
        days_back: int | None = None,
    ) -> DailyReading | None:
        """Fetch one explicit UK-local day and return it only when data is real."""
        _LOGGER.debug(
            "Fetching %s (%s) from %s to %s",
            resource_id,
            f"{days_back} day(s) back" if days_back is not None else day_start_uk.date(),
            day_start_uk,
            day_end_uk,
        )
        rows = await self._request_readings(
            resource_id,
            day_start_uk,
            day_end_uk,
            "PT30M",
        )
        valid = [row for row in rows if len(row) > 1 and row[1] is not None]
        total = sum(float(row[1]) for row in valid)
        if total <= 0:
            return None

        intervals = [
            (datetime.fromtimestamp(row[0], tz=timezone.utc), float(row[1]))
            for row in valid
        ]
        return DailyReading(
            day=day_start_uk.date().isoformat(),
            value=round(total, 3),
            intervals=intervals,
        )

    async def get_daily_reading(self, resource_id: str) -> DailyReading | None:
        """Return the latest completed non-zero day, allowing for API delay."""
        await self._ensure_authenticated()
        today_start_uk = datetime.now(UK_TZ).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        for days_back in range(1, 4):
            day_start_uk = today_start_uk - timedelta(days=days_back)
            day_end_uk = today_start_uk - timedelta(days=days_back - 1)
            reading = await self._fetch_day_reading(
                resource_id,
                day_start_uk,
                day_end_uk,
                days_back,
            )
            if reading is not None:
                return reading

        _LOGGER.warning(
            "No non-zero data found for %s in the last 3 completed days",
            resource_id,
        )
        return None

    @staticmethod
    def _next_month(value: datetime) -> datetime:
        if value.month == 12:
            return value.replace(year=value.year + 1, month=1, day=1)
        return value.replace(month=value.month + 1, day=1)

    async def _find_data_start(self, resource_id: str) -> datetime | None:
        """Find the first available day by exhaustively probing every API year."""
        await self._ensure_authenticated()
        now_utc = datetime.now(timezone.utc)
        earliest_year: int | None = None

        # P1Y is limited to 366 days, so query one calendar year at a time.
        # We deliberately scan every year back to the Unix epoch instead of
        # stopping after N empty windows: a long data gap must not hide older data.
        for year in range(now_utc.year, UNIX_EPOCH_YEAR - 1, -1):
            year_start = datetime(year, 1, 1, tzinfo=timezone.utc)
            year_end = min(
                datetime(year + 1, 1, 1, tzinfo=timezone.utc),
                now_utc,
            )
            if year_end <= year_start:
                continue
            rows = await self._request_readings(
                resource_id,
                year_start,
                year_end,
                "P1Y",
            )
            if any(
                len(row) > 1 and row[1] is not None and float(row[1]) > 0
                for row in rows
            ):
                earliest_year = year

        if earliest_year is None:
            return None

        year_start = datetime(earliest_year, 1, 1, tzinfo=timezone.utc)
        year_end = datetime(earliest_year + 1, 1, 1, tzinfo=timezone.utc)
        month_rows = await self._request_readings(
            resource_id,
            year_start,
            year_end,
            "P1M",
        )
        positive_months = [
            row
            for row in month_rows
            if len(row) > 1 and row[1] is not None and float(row[1]) > 0
        ]
        if not positive_months:
            return year_start.astimezone(UK_TZ) - timedelta(days=1)

        earliest_month = datetime.fromtimestamp(
            min(row[0] for row in positive_months),
            tz=timezone.utc,
        ).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_end = self._next_month(earliest_month)
        day_rows = await self._request_readings(
            resource_id,
            earliest_month,
            month_end,
            "P1D",
        )
        positive_days = [
            row
            for row in day_rows
            if len(row) > 1 and row[1] is not None and float(row[1]) > 0
        ]
        if not positive_days:
            return earliest_month.astimezone(UK_TZ) - timedelta(days=1)

        earliest_day_utc = datetime.fromtimestamp(
            min(row[0] for row in positive_days),
            tz=timezone.utc,
        )
        # Aggregate bucket boundaries are UTC here. Begin one UK-local day
        # earlier so DST/bucket alignment can never omit the first intervals.
        return earliest_day_utc.astimezone(UK_TZ).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ) - timedelta(days=1)

    async def _fetch_history_chunk(
        self,
        resource_id: str,
        start_uk: datetime,
        end_uk: datetime,
    ) -> list[DailyReading]:
        """Fetch a multi-day PT30M window and split it into UK-local days."""
        rows = await self._request_readings(
            resource_id,
            start_uk,
            end_uk,
            "PT30M",
        )
        by_day: dict[str, list[tuple[datetime, float]]] = {}
        for row in rows:
            if len(row) <= 1 or row[1] is None:
                continue
            timestamp = datetime.fromtimestamp(row[0], tz=timezone.utc)
            timestamp_uk = timestamp.astimezone(UK_TZ)
            if timestamp_uk < start_uk or timestamp_uk >= end_uk:
                continue
            day = timestamp_uk.date().isoformat()
            by_day.setdefault(day, []).append((timestamp, float(row[1])))

        readings: list[DailyReading] = []
        for day in sorted(by_day):
            intervals = sorted(by_day[day], key=lambda item: item[0])
            readings.append(
                DailyReading(
                    day=day,
                    value=round(sum(value for _, value in intervals), 3),
                    intervals=intervals,
                )
            )
        return readings

    async def get_available_daily_readings(
        self,
        resource_id: str,
    ) -> list[DailyReading]:
        """Fetch every complete historical day available for a resource."""
        await self._ensure_authenticated()
        start_uk = await self._find_data_start(resource_id)
        if start_uk is None:
            return []

        today_start_uk = datetime.now(UK_TZ).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        readings_by_day: dict[str, DailyReading] = {}
        chunk_start = start_uk
        while chunk_start < today_start_uk:
            chunk_end = min(
                chunk_start + timedelta(days=HISTORY_INTERVAL_DAYS),
                today_start_uk,
            )
            for reading in await self._fetch_history_chunk(
                resource_id,
                chunk_start,
                chunk_end,
            ):
                readings_by_day[reading.day] = reading
            chunk_start = chunk_end

        return [readings_by_day[day] for day in sorted(readings_by_day)]

    async def get_all_readings(self) -> dict[str, DailyReading | None]:
        if not self._resources:
            await self.discover_resources(self._virtual_entity_id)
        readings: dict[str, DailyReading | None] = {}
        for classifier, resource in self._resources.items():
            readings[classifier] = await self.get_daily_reading(
                resource["resource_id"]
            )
        return readings

    async def get_available_readings(
        self,
        classifiers: set[str] | None = None,
    ) -> dict[str, list[DailyReading]]:
        """Fetch all available history, optionally restricted to classifiers."""
        if not self._resources:
            await self.discover_resources(self._virtual_entity_id)
        result: dict[str, list[DailyReading]] = {}
        for classifier, resource in self._resources.items():
            if classifiers is not None and classifier not in classifiers:
                continue
            result[classifier] = await self.get_available_daily_readings(
                resource["resource_id"]
            )
        return result

    @property
    def resources(self) -> dict[str, dict[str, Any]]:
        return self._resources

    async def test_connection(self) -> bool:
        try:
            await self.authenticate()
            await self.discover_resources()
            return len(self._resources) > 0
        except (GlowmarktAuthError, GlowmarktApiError):
            return False
