"""Glowmarkt API client for Hildebrand Glow integration."""
from __future__ import annotations

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

# Glowmarkt rejects wider daily aggregate windows, so probe history in chunks.
CHUNK_PROBE_DAYS = 30
CHUNK_PROBE_LIMIT = 24


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

    async def _fetch_day_reading(
        self,
        resource_id: str,
        day_start_uk: datetime,
        day_end_uk: datetime,
        days_back: int | None = None,
    ) -> DailyReading | None:
        """Fetch one explicit UK-local day and return it only when data is real."""
        day_start_utc = day_start_uk.astimezone(timezone.utc)
        day_end_utc = day_end_uk.astimezone(timezone.utc)
        params = {
            "from": day_start_utc.strftime("%Y-%m-%dT%H:%M:%S"),
            "to": day_end_utc.strftime("%Y-%m-%dT%H:%M:%S"),
            "period": "PT30M",
            "offset": 0,
            "function": "sum",
        }
        _LOGGER.debug(
            "Fetching %s (%s) from %s to %s",
            resource_id,
            f"{days_back} day(s) back" if days_back is not None else day_start_uk.date(),
            params["from"],
            params["to"],
        )
        try:
            async with self._session.get(
                f"{GLOWMARKT_API_BASE}/resource/{resource_id}/readings",
                headers=self._get_headers(),
                params=params,
            ) as response:
                response.raise_for_status()
                data = await response.json()
                if data.get("status") != "OK" or not data.get("data"):
                    return None

                valid = [row for row in data["data"] if row[1] is not None]
                total = sum(row[1] for row in valid)
                if total <= 0:
                    return None

                intervals = [
                    (datetime.fromtimestamp(row[0], tz=timezone.utc), row[1])
                    for row in valid
                ]
                return DailyReading(
                    day=day_start_uk.date().isoformat(),
                    value=round(total, 3),
                    intervals=intervals,
                )
        except (ClientResponseError, ClientError) as err:
            _LOGGER.error(
                "Failed to get reading for %s (%s): %s",
                resource_id,
                day_start_uk.date(),
                err,
            )
            return None

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

    async def _find_data_start(self, resource_id: str) -> datetime | None:
        """Discover the earliest available day without guessing a fixed history size."""
        now_uk = datetime.now(UK_TZ).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        earliest: datetime | None = None
        consecutive_empty = 0

        for chunk in range(CHUNK_PROBE_LIMIT):
            chunk_end_uk = now_uk - timedelta(days=chunk * CHUNK_PROBE_DAYS)
            chunk_start_uk = chunk_end_uk - timedelta(days=CHUNK_PROBE_DAYS)
            try:
                async with self._session.get(
                    f"{GLOWMARKT_API_BASE}/resource/{resource_id}/readings",
                    headers=self._get_headers(),
                    params={
                        "from": chunk_start_uk.astimezone(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%S"
                        ),
                        "to": chunk_end_uk.astimezone(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%S"
                        ),
                        "period": "P1D",
                        "offset": 0,
                        "function": "sum",
                    },
                ) as response:
                    response.raise_for_status()
                    data = await response.json()
            except ClientError as err:
                _LOGGER.error("History probe failed for %s: %s", resource_id, err)
                break

            rows = data.get("data", []) if data.get("status") == "OK" else []
            nonzero = [
                row for row in rows if row[1] is not None and row[1] > 0
            ]
            if nonzero:
                earliest = datetime.fromtimestamp(
                    min(row[0] for row in nonzero),
                    tz=UK_TZ,
                ).replace(hour=0, minute=0, second=0, microsecond=0)
                consecutive_empty = 0
            else:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break

        return earliest

    async def get_available_daily_readings(
        self,
        resource_id: str,
    ) -> list[DailyReading]:
        """Fetch all complete historical days for a resource, oldest first."""
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
        readings: list[DailyReading] = []
        day_start_uk = start_uk
        while day_start_uk < today_start_uk:
            reading = await self._fetch_day_reading(
                resource_id,
                day_start_uk,
                day_start_uk + timedelta(days=1),
            )
            if reading is not None:
                readings.append(reading)
            day_start_uk += timedelta(days=1)
        return readings

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
