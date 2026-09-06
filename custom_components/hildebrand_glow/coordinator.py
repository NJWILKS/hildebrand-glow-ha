"""Data update coordinator for Hildebrand Glow integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DailyReading, GlowmarktApiClient, GlowmarktApiError, GlowmarktAuthError, UK_TZ
from .const import (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_GAS_CONSUMPTION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .identity import sensor_unique_id, site_identity

_LOGGER = logging.getLogger(__name__)
CUMULATIVE_CLASSIFIERS = (
    CLASSIFIER_ELECTRICITY_CONSUMPTION,
    CLASSIFIER_GAS_CONSUMPTION,
)
CUMULATIVE_STORAGE_VERSION = 1


class GlowmarktDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manage Glowmarkt data, cumulative energy values and history backfill."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_client: GlowmarktApiClient,
        tariff_config: dict[str, float],
        virtual_entity_id: str | None = None,
        entry_id: str = "default",
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.api_client = api_client
        self.tariff_config = tariff_config
        self._virtual_entity_id = virtual_entity_id
        self._entry_id = entry_id
        self._site_id = site_identity(virtual_entity_id, entry_id)
        self._resources: dict[str, dict[str, Any]] = {}
        self._last_readings: dict[str, DailyReading] = {}
        self._store = Store(
            hass,
            CUMULATIVE_STORAGE_VERSION,
            f"{DOMAIN}_{self._site_id}_cumulative",
        )
        self._cumulative: dict[str, Any] | None = None
        self._cumulative_lock = asyncio.Lock()
        self._backfill_started = False

    def _entity_id_for(self, classifier: str) -> str | None:
        registry = er.async_get(self.hass)
        unique_id = sensor_unique_id(self._site_id, classifier)
        return registry.async_get_entity_id("sensor", DOMAIN, unique_id)

    @staticmethod
    def _metadata_for(entity_id: str) -> StatisticMetaData:
        return StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name=None,
            source="recorder",
            statistic_id=entity_id,
            unit_class=None,
            unit_of_measurement="kWh",
        )

    def _import_hourly_statistics(
        self,
        entity_id: str,
        reading: DailyReading,
        baseline: float,
    ) -> float:
        """Import a day's true hourly shape and return its ending cumulative sum."""
        hourly: dict[datetime, float] = {}
        for timestamp, value in reading.intervals:
            hour_start = timestamp.replace(minute=0, second=0, microsecond=0)
            hourly[hour_start] = hourly.get(hour_start, 0.0) + value

        running = baseline
        stats: list[StatisticData] = []
        for hour_start in sorted(hourly):
            running += hourly[hour_start]
            stats.append(
                StatisticData(
                    start=hour_start,
                    state=round(hourly[hour_start], 3),
                    sum=round(running, 3),
                )
            )
        if stats:
            async_import_statistics(
                self.hass,
                self._metadata_for(entity_id),
                stats,
            )
        return round(running, 3)

    async def _load_cumulative(self) -> dict[str, Any]:
        if self._cumulative is None:
            self._cumulative = await self._store.async_load() or {}
        return self._cumulative

    async def _accumulate(self, classifier: str, reading: DailyReading) -> float:
        """Count each completed calendar day exactly once, across restarts."""
        async with self._cumulative_lock:
            cumulative = await self._load_cumulative()
            entry = cumulative.get(
                classifier,
                {"day": None, "cumulative": 0.0},
            )
            previous_day = entry.get("day")
            if previous_day is not None and reading.day <= previous_day:
                return float(entry["cumulative"])

            entity_id = self._entity_id_for(classifier)
            if entity_id is None or not reading.intervals:
                new_total = round(float(entry["cumulative"]) + reading.value, 3)
            else:
                new_total = self._import_hourly_statistics(
                    entity_id,
                    reading,
                    float(entry["cumulative"]),
                )

            cumulative[classifier] = {
                "day": reading.day,
                "cumulative": new_total,
            }
            await self._store.async_save(cumulative)
            return new_total

    def start_history_backfill(self) -> None:
        """Start backfill only after sensor entities exist; later polls can retry."""
        if self._backfill_started:
            return
        if not any(self._entity_id_for(c) for c in CUMULATIVE_CLASSIFIERS):
            return
        self._backfill_started = True
        self.hass.async_create_task(
            self._async_backfill_history(),
            name=f"{DOMAIN} history backfill",
        )

    async def _async_backfill_history(self) -> None:
        """Import all real hourly consumption history without blocking startup."""
        completed = False
        try:
            async with self._cumulative_lock:
                cumulative = await self._load_cumulative()
                if cumulative.get("_backfilled"):
                    completed = True
                    return

            history = await self.api_client.get_available_readings(
                set(CUMULATIVE_CLASSIFIERS)
            )

            async with self._cumulative_lock:
                cumulative = await self._load_cumulative()
                for classifier in CUMULATIVE_CLASSIFIERS:
                    entity_id = self._entity_id_for(classifier)
                    if entity_id is None:
                        _LOGGER.debug(
                            "Entity not registered for %s; backfill will retry",
                            classifier,
                        )
                        return

                    baseline = 0.0
                    entry = cumulative.get(
                        classifier,
                        {"day": None, "cumulative": 0.0},
                    )
                    for reading in history.get(classifier, []):
                        baseline = self._import_hourly_statistics(
                            entity_id,
                            reading,
                            baseline,
                        )
                        entry = {
                            "day": reading.day,
                            "cumulative": baseline,
                        }
                    cumulative[classifier] = entry

                    # Replace state-compiler artefacts for the current UK day
                    # with a flat corrected baseline until that day completes.
                    today_start_uk = datetime.now(UK_TZ).replace(
                        hour=0,
                        minute=0,
                        second=0,
                        microsecond=0,
                    )
                    today_start_utc = today_start_uk.astimezone(timezone.utc)
                    current_hour_utc = datetime.now(timezone.utc).replace(
                        minute=0,
                        second=0,
                        microsecond=0,
                    )
                    hours_elapsed = max(
                        0,
                        int(
                            (current_hour_utc - today_start_utc).total_seconds()
                            // 3600
                        )
                        + 1,
                    )
                    if hours_elapsed:
                        gap_stats = [
                            StatisticData(
                                start=today_start_utc + timedelta(hours=hour),
                                state=0.0,
                                sum=float(entry["cumulative"]),
                            )
                            for hour in range(hours_elapsed)
                        ]
                        async_import_statistics(
                            self.hass,
                            self._metadata_for(entity_id),
                            gap_stats,
                        )

                cumulative["_backfilled"] = True
                await self._store.async_save(cumulative)
                completed = True
                _LOGGER.info(
                    "Backfilled Glowmarkt history: %s",
                    ", ".join(
                        f"{classifier}={len(readings)} day(s)"
                        for classifier, readings in history.items()
                    ),
                )
        except Exception:  # Background work must not create an unhandled task error.
            _LOGGER.exception("Glowmarkt history backfill failed")
        finally:
            if not completed:
                self._backfill_started = False

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            if not self._resources:
                self._resources = await self.api_client.discover_resources(
                    self._virtual_entity_id
                )

            readings = await self.api_client.get_all_readings()
            for classifier, reading in readings.items():
                if reading is not None:
                    self._last_readings[classifier] = reading

            merged = {
                classifier: self._last_readings.get(classifier)
                for classifier in readings
            }
            values = {
                classifier: reading.value if reading is not None else None
                for classifier, reading in merged.items()
            }

            cumulative_readings: dict[str, float | None] = {}
            for classifier in CUMULATIVE_CLASSIFIERS:
                reading = merged.get(classifier)
                cumulative_readings[classifier] = (
                    await self._accumulate(classifier, reading)
                    if reading is not None
                    else None
                )

            data: dict[str, Any] = {
                "readings": values,
                "cumulative_readings": cumulative_readings,
                "resources": self._resources,
                "costs": {},
            }

            electricity = values.get(CLASSIFIER_ELECTRICITY_CONSUMPTION)
            if electricity is not None:
                data["costs"]["electricity"] = round(
                    electricity * self.tariff_config.get("electricity_rate", 0)
                    + self.tariff_config.get("electricity_standing_charge", 0),
                    2,
                )

            gas = values.get(CLASSIFIER_GAS_CONSUMPTION)
            if gas is not None:
                data["costs"]["gas"] = round(
                    gas * self.tariff_config.get("gas_rate", 0)
                    + self.tariff_config.get("gas_standing_charge", 0),
                    2,
                )

            data["costs"]["total"] = round(
                data["costs"].get("electricity", 0)
                + data["costs"].get("gas", 0),
                2,
            )
            data["costs"]["standing_charges_total"] = round(
                self.tariff_config.get("electricity_standing_charge", 0)
                + self.tariff_config.get("gas_standing_charge", 0),
                2,
            )

            self.start_history_backfill()
            return data
        except GlowmarktAuthError as err:
            raise UpdateFailed(f"Authentication error: {err}") from err
        except GlowmarktApiError as err:
            raise UpdateFailed(f"API error: {err}") from err

    @property
    def resources(self) -> dict[str, dict[str, Any]]:
        return self._resources

    def update_tariff_config(self, tariff_config: dict[str, float]) -> None:
        self.tariff_config = tariff_config

    def clear_daily_cache(self) -> None:
        self._last_readings.clear()
