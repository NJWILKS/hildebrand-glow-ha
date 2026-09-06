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

# Hildebrand documents a maximum PT30M query span of 10 days. Use nine UK-local
# calendar days so a 25-hour autumn DST day can never make the UTC span exceed
# that API limit.
HISTORY_INTERVAL_DAYS = 9

# Keep all requests for one Bright account in a single, paced lane. This prevents
# normal polling and a history backfill from producing a burst of concurrent API
# calls. 429 and transient server responses additionally honour Retry-After when
# supplied and otherwise use bounded exponential backoff.
API_MAX_RETRIES = 4
API_MIN_REQUEST_SPACING_SECONDS = 0.5
API_MAX_BACKOFF_SECONDS = 30.0
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


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
        self._request_lock = asyncio.Lock()
        self._next_request_at = 0.0

    @staticmethod
    def _retry_delay(response: Any, attempt: int) -> float:
        headers = getattr(response, "headers", {}) or {}
        retry_after = headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after is not None else float(2**attempt)
        except (TypeError, ValueError):
            delay = float(2**attempt)
        return min(max(delay, 0.0), API_MAX_BACKOFF_SECONDS)

    async def _wait_for_request_slot(self) -> None:
        loop = asyncio.get_running_loop()
        delay = self._next_request_at - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        self._next_request_at = loop.time() + API_MIN_REQUEST_SPACING_SECONDS

    async def authenticate(self) -> bool:
        """Authenticate with bounded retry/backoff for rate limits and server errors."""
        headers = {
            "Content-Type": "application/json",
            "applicationId": GLOWMARKT_APP_ID,
        }
        payload = {"username": self._username, "password": self._password}

        for attempt in range(API_MAX_RETRIES + 1):
            try:
                async with self._request_lock:
                    await self._wait_for_request_slot()
                    async with self._session.post(
                        f"{GLOWMARKT_API_BASE}/auth",
                        headers=headers,
                        json=payload,
                    ) as response:
                        if response.status == 401:
                            raise GlowmarktAuthError("Invalid username or password")

                        if response.status in TRANSIENT_HTTP_STATUSES:
                            if attempt >= API_MAX_RETRIES:
                                raise GlowmarktApiError(
                                    "Glowmarkt authentication temporarily unavailable"
                                )
                            delay = self._retry_delay(response, attempt)
                            self._next_request_at = max(
                                self._next_request_at,
                                asyncio.get_running_loop().time() + delay,
                            )
                            _LOGGER.warning(
                                "Glowmarkt temporarily rejected authentication; "
                                "retrying with backoff"
                            )
                            continue

                        if response.status >= 400:
                            raise GlowmarktAuthError(
                                f"Authentication failed with HTTP {response.status}"
                            )

                        data = await response.json()

                if data.get("valid") and data.get("token"):
                    self._token = data["token"]
                    self._token_expiry = datetime.now() + timedelta(days=6)
                    return True
                raise GlowmarktAuthError("Authentication failed: invalid response")
            except GlowmarktAuthError:
                raise
            except GlowmarktApiError:
                raise
            except ClientResponseError as err:
                raise GlowmarktApiError(
                    f"Authentication request failed: HTTP {err.status}"
                ) from err
            except ClientError as err:
                raise GlowmarktApiError(f"Connection error: {err}") from err

        raise GlowmarktApiError("Authentication retries exhausted")

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

    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        allow_not_found: bool = False,
    ) -> Any | None:
        """GET JSON through the shared paced lane with bounded transient retries."""
        for attempt in range(API_MAX_RETRIES + 1):
            try:
                async with self._request_lock:
                    await self._wait_for_request_slot()
                    async with self._session.get(
                        url,
                        headers=self._get_headers(),
                        params=params,
                    ) as response:
                        if response.status == 404 and allow_not_found:
                            return None

                        if response.status in TRANSIENT_HTTP_STATUSES:
                            if attempt >= API_MAX_RETRIES:
                                raise GlowmarktApiError(
                                    "Glowmarkt request failed after retries: "
                                    f"HTTP {response.status}"
                                )
                            delay = self._retry_delay(response, attempt)
                            self._next_request_at = max(
                                self._next_request_at,
                                asyncio.get_running_loop().time() + delay,
                            )
                            _LOGGER.warning(
                                "Glowmarkt request rate-limited or temporarily "
                                "unavailable; retrying with backoff"
                            )
                            continue

                        response.raise_for_status()
                        return await response.json()
            except GlowmarktApiError:
                raise
            except ClientResponseError as err:
                raise GlowmarktApiError(
                    f"Glowmarkt request failed: HTTP {err.status}"
                ) from err
            except ClientError as err:
                raise GlowmarktApiError(f"Glowmarkt connection error: {err}") from err

        raise GlowmarktApiError("Glowmarkt request retries exhausted")

    async def get_virtual_entities(self) -> list[dict[str, Any]]:
        await self._ensure_authenticated()
        data = await self._get_json(f"{GLOWMARKT_API_BASE}/virtualentity")
        return data if isinstance(data, list) else []

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
            data = await self._get_json(
                f"{GLOWMARKT_API_BASE}/virtualentity/{ve_id}/resources"
            )
            if not isinstance(data, dict):
                continue
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
        return self._resources

    async def get_first_reading_time(self, resource_id: str) -> datetime | None:
        """Return the UTC time of the first available reading for a resource."""
        await self._ensure_authenticated()
        data = await self._get_json(
            f"{GLOWMARKT_API_BASE}/resource/{resource_id}/first-time",
            allow_not_found=True,
        )
        if data is None:
            return None
        if not isinstance(data, dict) or data.get("status") not in (None, "OK"):
            raise GlowmarktApiError("Glowmarkt first-time query returned an error")
        first_ts = (data.get("data") or {}).get("firstTs")
        if first_ts is None:
            return None
        return datetime.fromtimestamp(float(first_ts), tz=timezone.utc)

    async def get_last_reading_time(self, resource_id: str) -> datetime | None:
        """Return the UTC time of the most recent available reading for a resource."""
        await self._ensure_authenticated()
        data = await self._get_json(
            f"{GLOWMARKT_API_BASE}/resource/{resource_id}/last-time",
            allow_not_found=True,
        )
        if data is None:
            return None
        if not isinstance(data, dict) or data.get("status") not in (None, "OK"):
            raise GlowmarktApiError("Glowmarkt last-time query returned an error")
        last_ts = (data.get("data") or {}).get("lastTs")
        if last_ts is None:
            return None
        return datetime.fromtimestamp(float(last_ts), tz=timezone.utc)

    async def _request_readings(
        self,
        resource_id: str,
        start: datetime,
        end: datetime,
        period: str,
    ) -> list[list[Any]]:
        """Fetch a readings window without interpreting API failures as no data."""
        params = {
            "from": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "to": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "period": period,
            "offset": 0,
            "function": "sum",
            "nulls": 1,
        }
        data = await self._get_json(
            f"{GLOWMARKT_API_BASE}/resource/{resource_id}/readings",
            params=params,
        )
        if not isinstance(data, dict) or data.get("status") != "OK":
            raise GlowmarktApiError("Glowmarkt readings query returned an error")
        rows = data.get("data", [])
        return rows if isinstance(rows, list) else []

    async def _fetch_day_reading(
        self,
        resource_id: str,
        day_start_uk: datetime,
        day_end_uk: datetime,
        days_back: int | None = None,
    ) -> DailyReading | None:
        """Fetch one explicit UK-local day when the API has real data for it."""
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
        if not valid:
            return None

        intervals = [
            (datetime.fromtimestamp(row[0], tz=timezone.utc), float(row[1]))
            for row in valid
        ]
        return DailyReading(
            day=day_start_uk.date().isoformat(),
            value=round(sum(value for _, value in intervals), 3),
            intervals=intervals,
        )

    async def get_daily_reading(self, resource_id: str) -> DailyReading | None:
        """Return the latest completed day that contains actual API readings."""
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
            "No readings found for %s in the last 3 completed days",
            resource_id,
        )
        return None

    async def _find_data_start(self, resource_id: str) -> datetime | None:
        """Find the first UK-local day using Glowmarkt's authoritative endpoint."""
        first_time = await self.get_first_reading_time(resource_id)
        if first_time is None:
            return None
        return first_time.astimezone(UK_TZ).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

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

    async def get_readings(
        self,
        classifiers: set[str] | None = None,
    ) -> dict[str, DailyReading | None]:
        """Fetch latest completed-day readings for selected discovered resources."""
        if not self._resources:
            await self.discover_resources(self._virtual_entity_id)
        readings: dict[str, DailyReading | None] = {}
        for classifier, resource in self._resources.items():
            if classifiers is not None and classifier not in classifiers:
                continue
            readings[classifier] = await self.get_daily_reading(
                resource["resource_id"]
            )
        return readings

    async def get_all_readings(self) -> dict[str, DailyReading | None]:
        """Backward-compatible wrapper that fetches all discovered resources."""
        return await self.get_readings()

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
